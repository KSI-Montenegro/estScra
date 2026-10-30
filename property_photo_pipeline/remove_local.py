#!/usr/bin/env python3
"""Local, masked, trial-first repair. No model download or hosted edit API."""
import argparse
import csv
import io
import json
import os
import re
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote

import numpy as np
from PIL import Image, ImageOps

from pipeline import CACHE_CONTROL, atomic_write, group, optimize, r2_settings, sha, write_json

MAX_BYTES = 400000
ALGORITHM = 'local-lama-mask-v2-edge-pad'
BRANDS = ('patuljak', 'estitor', 'realitica')


def repair_rgb(original, mask, infer, context=128):
    """Crop context, pad to 8, infer, and composite only masked pixels."""
    original = np.asarray(original)
    mask = np.asarray(mask)
    if original.ndim != 3 or original.shape[2] != 3 or original.dtype != np.uint8:
        raise ValueError('original must be RGB uint8')
    if mask.shape != original.shape[:2] or mask.dtype != np.uint8:
        raise ValueError('mask must be uint8 and match the image dimensions')
    binary = mask > 0
    ys, xs = np.nonzero(binary)
    if not len(xs):
        raise ValueError('mask is empty')
    if context < 0:
        raise ValueError('context must be nonnegative')
    h, w = binary.shape
    x1, y1 = max(0, int(xs.min()) - context), max(0, int(ys.min()) - context)
    x2, y2 = min(w, int(xs.max()) + 1 + context), min(h, int(ys.max()) + 1 + context)
    crop, cmask = original[y1:y2, x1:x2], binary[y1:y2, x1:x2]
    ph, pw = (-crop.shape[0]) % 8, (-crop.shape[1]) % 8
    image_pad = np.pad(crop, ((0, ph), (0, pw), (0, 0)), mode='edge')
    mask_pad = np.pad(cmask, ((0, ph), (0, pw)), mode='edge')
    generated = np.asarray(infer(image_pad, mask_pad.astype(np.uint8)))
    if generated.shape != image_pad.shape or generated.dtype != np.uint8:
        raise ValueError('model output must be RGB uint8 with padded input dimensions')
    composite = original.copy()
    target = composite[y1:y2, x1:x2]
    target[cmask] = generated[:crop.shape[0], :crop.shape[1]][cmask]
    if not np.array_equal(composite[~binary], original[~binary]):
        raise ValueError('outside-mask pixels changed')
    return composite, [x1, y1, x2, y2]


class TorchInpainter:
    def __init__(self, path, threads):
        import torch
        self.torch = torch
        torch.set_num_threads(threads)
        self.model = torch.jit.load(str(path), map_location='cpu').eval()

    def __call__(self, rgb, mask):
        torch = self.torch
        image = torch.from_numpy(np.ascontiguousarray(rgb.transpose(2, 0, 1))).float().unsqueeze(0) / 255.0
        mask_t = torch.from_numpy(np.ascontiguousarray(mask)).float()[None, None]
        with torch.inference_mode():
            result = self.model(image, mask_t)
        if isinstance(result, dict):
            result = result.get('inpainted')
        if not isinstance(result, torch.Tensor) or tuple(result.shape) != tuple(image.shape):
            raise ValueError('unsupported LaMa model output; expected tensor or inpainted tensor')
        if not torch.isfinite(result).all():
            raise ValueError('non-finite LaMa output')
        return (result[0].clamp(0, 1).permute(1, 2, 0).cpu().numpy() * 255).round().astype(np.uint8)


def focused_brand_ocr(rgb, box, binary='tesseract', timeout=30):
    x1, y1, x2, y2 = box
    crop = Image.fromarray(rgb[y1:y2, x1:x2])
    image = io.BytesIO()
    crop.save(image, format='PNG')
    env = os.environ.copy()
    env['OMP_THREAD_LIMIT'] = '1'
    proc = subprocess.run([binary, 'stdin', 'stdout', '--oem', '1', '--psm', '11', '-l', 'eng', 'tsv'],
                          input=image.getvalue(), capture_output=True, timeout=timeout, env=env)
    if proc.returncode:
        raise ValueError('focused OCR failed')
    tokens = []
    lines = {}
    for row in csv.DictReader(io.StringIO(proc.stdout.decode('utf-8', errors='replace')), delimiter='\t'):
        token = re.sub(r'[^a-z0-9]', '', row.get('text', '').lower())
        try:
            confidence = float(row.get('conf', '-1'))
        except ValueError:
            continue
        if token and confidence >= 45:
            tokens.append(token)
            line = tuple(row.get(k, '') for k in ('page_num', 'block_num', 'par_num', 'line_num'))
            lines.setdefault(line, []).append(token)
    tokens += [''.join(parts) for parts in lines.values() if len(parts) > 1]
    for token in tokens:
        for brand in BRANDS:
            if len(token) >= 4 and (brand in token or (len(token) <= len(brand) * 2 and SequenceMatcher(None, token, brand).ratio() >= .82)):
                return {'brand_detected': True, 'matched_token': token, 'brand': brand}
    return {'brand_detected': False, 'matched_token': '', 'brand': ''}


def process_row(row, cache, output, build_mask, infer, template, model_hash, config_hash, context=128,
                threshold=.85, ocr=focused_brand_ocr, shape_templates=None):
    """Process one exact source; write private evidence and fail closed on QC."""
    base = {k: row.get(k, '') for k in ('property_key', 'image_number', 'image_url', 'source_sha256', 'triage_group', 'candidate_method')}
    base.update(status='error', model_sha256=model_hash, config_sha256=config_hash, algorithm=ALGORITHM,
                certification='not_certified', uploaded_url='', error='')
    try:
        source, ident = group(row['property_key'])
        number = int(row['image_number'])
        if number < 1:
            raise ValueError('image number must be positive')
        raw = (Path(cache) / (sha(row['image_url'].encode())[:24] + '.image')).read_bytes()
        if sha(raw) != row['source_sha256']:
            raise ValueError('original SHA256 mismatch')
        with Image.open(io.BytesIO(raw)) as im:
            original = np.asarray(ImageOps.exif_transpose(im).convert('RGB'))
        mask, mask_meta = build_mask(row, original)
        if not isinstance(mask_meta, dict) or mask_meta.get('eligible') is False:
            base.update(status='queued', error='mask not eligible', mask_metadata=mask_meta)
            return base
        mask = np.asarray(mask)
        repaired, region = repair_rgb(original, mask, infer, context)
        if repaired.shape != original.shape:
            raise ValueError('dimensions changed')
        from match_known_logo import match_image
        residual_score, residual_box, residual_scale = match_image(repaired, template)
        box = mask_meta.get('overlay_box_original_pixels')
        if not isinstance(box, (list, tuple)) or len(box) != 4:
            raise ValueError('mask metadata requires overlay_box_original_pixels')
        x1, y1, x2, y2 = map(int, box)
        height, width = original.shape[:2]
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError('invalid overlay bounds')
        ocr_region = [max(0, x1-4), max(0, y1-4), min(width, x2+4), min(height, y2+4)]
        ocr_result = ocr(repaired, ocr_region)
        if not isinstance(ocr_result, dict) or 'brand_detected' not in ocr_result:
            raise ValueError('invalid focused OCR result')
        qc_ok = residual_score < threshold and not ocr_result['brand_detected']
        text_shape = {'status': 'unavailable'}
        if shape_templates is not None:
            from text_shape_metric import shape_scores
            box = mask_meta.get('graphic_box_original_pixels')
            text_shape = {'status': 'diagnostic_only_unvalidated',
                          'source': shape_scores(original, box, shape_templates),
                          'edited': shape_scores(repaired, box, shape_templates)}
        outdir = Path(output)
        name = f'{number:03d}-{row["source_sha256"]}'
        intermediate = outdir / 'intermediate' / source / ident / (name + '.png')
        png = io.BytesIO()
        Image.fromarray(repaired).save(png, 'PNG')
        atomic_write(intermediate, png.getvalue())
        base.update(mask_metadata=mask_meta, mask_sha256=sha(mask.tobytes()), region=region, ocr_region=ocr_region,
                    intermediate_path=str(intermediate.resolve()), intermediate_sha256=sha(png.getvalue()),
                    source_oriented_dimensions=[original.shape[1], original.shape[0]],
                    outside_mask_unchanged_before_encoding=True, residual_logo_score=residual_score,
                    residual_logo_box=residual_box, residual_logo_scale=residual_scale, focused_ocr=ocr_result,
                    residual_text_shape=text_shape,
                    qc='mascot_and_ocr_only_pass_text_shape_unvalidated' if qc_ok else 'mascot_or_ocr_residual_detected')
        if not qc_ok:
            base.update(status='queued', error='residual mascot or brand text detected')
            return base
        encoded, size, original_size, quality = optimize(png.getvalue(), 300000, MAX_BYTES, 75)
        if size != original_size:
            base.update(status='queued', error='byte limit would require resizing original dimensions')
            return base
        digest = sha(encoded)
        key = f'properties/{source}/{ident}/cleaned/{number:03d}-{digest}.webp'
        path = outdir / 'ready' / key
        atomic_write(path, encoded)
        base.update(status='processed', output_path=str(path.resolve()), output_sha256=digest,
                    output_bytes=len(encoded), output_dimensions=list(size), quality=quality, key=key,
                    error='', upload_status='not_uploaded')
        return base
    except Exception as exc:
        base['error'] = type(exc).__name__ + ': ' + str(exc)
        return base


def upload_processed(records, bucket, client, budget, public_base, workers=16, on_result=None):
    """Preflight bucket and matching keys, then upload conditionally with bounded parallelism."""
    from upload_staged import retry
    if not 1 <= workers <= 16:
        raise ValueError('upload workers must be 1–16')
    used, keys = 0, set()
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        for obj in page.get('Contents', []):
            used += obj['Size']
            keys.add(obj['Key'])
    ready = [r for r in records if r['status'] == 'processed']
    if len({r['key'] for r in ready}) != len(ready):
        raise ValueError('duplicate cleaned object key')
    new_bytes = sum(r['output_bytes'] for r in ready if r['key'] not in keys)
    if budget < 0 or used + new_bytes > budget:
        raise ValueError('bucket budget exceeded')

    def check_existing(row):
        head = retry(lambda: client.head_object(Bucket=bucket, Key=row['key']))
        meta = {'sha256': row['output_sha256'], 'source-sha256': row['source_sha256'], 'status': 'processed'}
        if head.get('ContentLength') != row['output_bytes'] or any(head.get('Metadata', {}).get(k) != v for k, v in meta.items()):
            raise ValueError('existing cleaned object conflicts')

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(check_existing, [r for r in ready if r['key'] in keys]))
    print(f'Upload preflight: bucket bytes {used}, new bytes {new_bytes}, images {len(ready)}', flush=True)

    def worker(row):
        data = Path(row['output_path']).read_bytes()
        if sha(data) != row['output_sha256'] or len(data) != row['output_bytes'] or len(data) > MAX_BYTES:
            raise ValueError('processed bytes changed before upload')
        if row['key'] in keys:
            return 'existing'
        meta = {'sha256': row['output_sha256'], 'source-sha256': row['source_sha256'], 'status': 'processed'}
        try:
            retry(lambda: client.put_object(Bucket=bucket, Key=row['key'], Body=data, ContentType='image/webp',
                                           CacheControl=CACHE_CONTROL, Metadata=meta, IfNoneMatch='*'))
            return 'uploaded'
        except Exception:
            # Resolve conditional-write races or lost responses only if the object matches.
            check_existing(row)
            return 'existing'

    started = time.monotonic()
    counts = Counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, row): row for row in ready}
        for i, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                row['upload_status'] = future.result()
                row['uploaded_url'] = public_base.rstrip('/') + '/' + quote(row['key'], safe='/') if public_base else ''
            except Exception as exc:
                row['upload_status'] = 'error'
                row['upload_error'] = type(exc).__name__
                row['uploaded_url'] = ''
            counts[row['upload_status']] += 1
            if on_result:
                on_result(row)
            if i % 10 == 0 or i == len(ready):
                print(f'Upload {i}/{len(ready)} in {time.monotonic()-started:.1f}s: {dict(counts)}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', default='property_photo_pipeline/reports/watermark_candidates.csv')
    parser.add_argument('--cache', default='watermark_detection/cache')
    parser.add_argument('--output', default='property_photo_pipeline/staging/local_removal')
    parser.add_argument('--model', default=os.environ.get('LAMA_MODEL_PATH', ''))
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--upload', action='store_true')
    parser.add_argument('--max-images', type=int, default=3)
    parser.add_argument('--threads', type=int, default=2, choices=(2, 4))
    parser.add_argument('--workers', type=int, default=1, choices=(1, 2))
    parser.add_argument('--context', type=int, default=128)
    parser.add_argument('--budget-bytes', type=int, default=8_000_000_000)
    args = parser.parse_args()
    if args.max_images < 1 or args.context < 0 or (args.upload and not args.execute) or args.workers * args.threads > 4:
        parser.error('max-images must be positive, context nonnegative, upload requires execute, and workers * threads must not exceed 4')
    with open(args.candidates, newline='', encoding='utf-8-sig') as f:
        candidates = list(csv.DictReader(f))
    eligible = candidates
    seen = set()
    for row in eligible:
        try:
            group(row['property_key'])
            number = int(row['image_number'])
            key = (row['property_key'], number)
            if number <= 0 or key in seen:
                raise ValueError('duplicate or invalid image number')
            seen.add(key)
        except (KeyError, ValueError) as exc:
            parser.error('invalid candidate inventory: ' + str(exc))
    print(f'{len(eligible)} candidates for mask eligibility; trial limit {args.max_images}.', flush=True)
    if not args.execute:
        print('Dry run: no model loaded and no images processed.')
        return 0
    if not args.model:
        parser.error('--model or LAMA_MODEL_PATH is required for execution')
    from watermark_mask import build_mask
    model_hash = sha(Path(args.model).read_bytes())
    template_path = Path(__file__).with_name('reference_assets') / 'patuljak_mascot.png'
    template = np.asarray(Image.open(template_path).convert('RGB'))
    asset_dir = Path(__file__).with_name('reference_assets')
    tracked_assets = ('patuljak_overlay_mask.png', 'patuljak_overlay_mask.json', 'patuljak_word.png', 'patuljak_dotme.png')
    asset_hashes = {name: sha((asset_dir / name).read_bytes()) for name in tracked_assets}
    shape_templates = {name: np.asarray(Image.open(asset_dir / filename).convert('RGB')) for name, filename in
                       (('word', 'patuljak_word.png'), ('dotme', 'patuljak_dotme.png'))}
    settings = {'algorithm': ALGORITHM, 'model': model_hash, 'context': args.context, 'threads': args.threads, 'threshold': .85,
                'template': sha(template_path.read_bytes()), 'mask_module': sha(Path(__import__('watermark_mask').__file__).read_bytes()),
                'shape_metric_module': sha(Path(__file__).with_name('text_shape_metric.py').read_bytes()), 'mask_and_text_assets': asset_hashes}
    config_hash = sha(json.dumps(settings, sort_keys=True).encode())
    infer = TorchInpainter(args.model, args.threads)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    records = []
    checkpoint = out / 'checkpoint.jsonl'
    recovered = {}
    if checkpoint.exists():
        for line in checkpoint.read_text().splitlines():
            try:
                rec = json.loads(line)
                if rec.get('config_sha256') == config_hash and rec.get('status') == 'processed':
                    path = Path(rec['output_path'])
                    source_path = Path(args.cache) / (sha(rec['image_url'].encode())[:24] + '.image')
                    if path.is_file() and sha(path.read_bytes()) == rec['output_sha256'] and source_path.is_file() and sha(source_path.read_bytes()) == rec['source_sha256']:
                        recovered[(rec['property_key'], rec['image_number'], rec['source_sha256'])] = rec
            except (ValueError, OSError, KeyError):
                continue
    selected = eligible[:args.max_images]
    started = time.monotonic()
    counts = Counter()
    def run_job(row):
        began = time.monotonic()
        identity = (row['property_key'], row['image_number'], row['source_sha256'])
        record = recovered.get(identity) or process_row(row, args.cache, out, build_mask, infer, template,
                                                        model_hash, config_hash, args.context, shape_templates=shape_templates)
        record['job_elapsed_seconds'] = round(time.monotonic() - began, 3)
        return record
    with checkpoint.open('a', encoding='utf-8') as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, record in enumerate(pool.map(run_job, selected), 1):
            records.append(record)
            counts[record['status']] += 1
            log.write(json.dumps(record) + '\n')
            log.flush()
            write_json(out / 'records' / record['property_key'] / (str(record['image_number']).zfill(3) + '.json'), record)
            if i % 10 == 0 or i == len(selected):
                print(f'Processed {i}/{len(selected)} in {time.monotonic()-started:.1f}s: {dict(counts)}', flush=True)
    write_json(out / 'summary.json', {'algorithm': ALGORITHM, 'settings': settings, 'records': records, 'queued_without_validated_mask': sum(r['status'] == 'queued' and r.get('error') == 'mask not eligible' for r in records), 'not_attempted': len(candidates) - len(records),
               'meaning': 'Automated residual checks only; scene quality and watermark absence are not certified.'})
    if args.upload:
        if not os.environ.get('R2_ACCESS_KEY_ID') or not os.environ.get('R2_SECRET_ACCESS_KEY'):
            raise ValueError('R2 credentials missing')
        import boto3
        config = r2_settings()
        client = boto3.client('s3', endpoint_url=config['endpoint_url'], region_name='auto',
                              aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'])
        def checkpoint_upload(record):
            with checkpoint.open('a', encoding='utf-8') as log:
                log.write(json.dumps(record) + '\n')
                log.flush()
            write_json(out / 'records' / record['property_key'] / (str(record['image_number']).zfill(3) + '.json'), record)
        upload_processed(records, config['bucket_name'], client, args.budget_bytes, config['public_base_url'],
                         workers=16, on_result=checkpoint_upload)
        write_json(out / 'summary.json', {'algorithm': ALGORITHM, 'settings': settings, 'records': records, 'queued_without_validated_mask': sum(r['status'] == 'queued' and r.get('error') == 'mask not eligible' for r in records), 'not_attempted': len(candidates) - len(records),
                   'meaning': 'Automated residual checks only; scene quality and watermark absence are not certified.'})
    return 2 if any(r['status'] == 'error' or r.get('upload_status') == 'error' for r in records) else 0


if __name__ == '__main__':
    raise SystemExit(main())
