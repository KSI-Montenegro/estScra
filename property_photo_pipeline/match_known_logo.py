#!/usr/bin/env python3
"""Find candidate instances of a reference mascot in captured images; no clearance is implied."""
import argparse
import csv
import hashlib
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

from pipeline import atomic_write, sha, write_json

cv2.setNumThreads(1)
SCALES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
FIELDS = ['property_key', 'image_number', 'image_url', 'source_sha256', 'ocr_status',
          'match_status', 'score', 'bbox_xyxy', 'template_scale', 'error']


def match_image(image_rgb, template_rgb, scales=SCALES):
    """RGB normalized correlation, restricted to the lower 35%, with original coordinates."""
    height, width = image_rgb.shape[:2]
    y_start = int(height * .65)
    roi = image_rgb[y_start:, :]
    best_score, best_box, best_scale = -1.0, None, None
    for scale in scales:
        w = max(1, round(template_rgb.shape[1] * scale))
        h = max(1, round(template_rgb.shape[0] * scale))
        if w > roi.shape[1] or h > roi.shape[0] or w < 2 or h < 2:
            continue
        template = cv2.resize(template_rgb, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        scores = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
        _, value, _, (x, y) = cv2.minMaxLoc(scores)
        if math.isfinite(value) and value > best_score:
            best_score, best_box, best_scale = float(value), [x, y_start + y, x + w, y_start + y + h], scale
    if best_box is None:
        raise ValueError('image too small for any template scale')
    return best_score, best_box, best_scale


def sample_rows(rows, n):
    if not n:
        return rows
    groups = {}
    for row in rows:
        groups.setdefault(row['property_key'].split('_', 1)[0], []).append(row)
    # Balanced by listing source, ordered as in the input for reproducibility.
    return [row for source in sorted(groups) for row in groups[source][:n]]


def scan_one(row, cache, template, threshold):
    result = {k: row.get(k, '') for k in ('property_key', 'image_number', 'image_url', 'source_sha256')}
    result.update(ocr_status=row.get('status', ''), match_status='unavailable', score='', bbox_xyxy='', template_scale='', error='')
    if not row.get('source_sha256'):
        result['error'] = 'captured original unavailable'
        return result
    try:
        path = cache / (sha(row['image_url'].encode('utf-8'))[:24] + '.image')
        data = path.read_bytes()
        if sha(data) != row['source_sha256']:
            raise ValueError('captured original SHA256 mismatch')
        with Image.open(io.BytesIO(data)) as image:
            rgb = np.asarray(ImageOps.exif_transpose(image).convert('RGB'))
        score, box, scale = match_image(rgb, template)
        result.update(match_status='known_logo_template_candidate' if score >= threshold else 'no_template_match',
                      score=round(score, 6), bbox_xyxy=json.dumps(box), template_scale=scale)
    except (OSError, ValueError, cv2.error) as exc:
        result.update(match_status='error', error=str(exc))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr-csv', default='watermark_detection/runs/ocr_full/ocr_results.csv')
    parser.add_argument('--cache', default='watermark_detection/cache')
    parser.add_argument('--output', default='watermark_detection/runs/logo_match')
    parser.add_argument('--template-config', default='property_photo_pipeline/reference_assets/patuljak_mascot.json')
    parser.add_argument('--threshold', type=float, default=.85)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--pilot-per-source', type=int, default=0, help='Use the first N rows from each source')
    args = parser.parse_args()
    if not 1 <= args.workers <= 4 or not .0 < args.threshold <= 1 or args.pilot_per_source < 0:
        parser.error('Require workers 1–4, threshold in (0, 1], and a nonnegative pilot count')
    config_path = Path(args.template_config)
    config = json.loads(config_path.read_text())
    template_bytes = (config_path.parent / config['template_file']).read_bytes()
    if sha(template_bytes) != config['template_sha256']:
        raise ValueError('template hash mismatch')
    template = np.asarray(Image.open(io.BytesIO(template_bytes)).convert('RGB'))
    with open(args.ocr_csv, newline='', encoding='utf-8-sig') as file:
        rows = sample_rows(list(csv.DictReader(file)), args.pilot_per_source)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / 'checkpoint.jsonl'
    identity = sha(json.dumps({'template': config['template_sha256'], 'threshold': args.threshold, 'scales': SCALES, 'roi_start': .65}).encode())
    recovered = {}
    if checkpoint.exists():
        with checkpoint.open(encoding='utf-8') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('_algorithm') == identity and rec.get('match_status') != 'error':
                    recovered[(rec['property_key'], rec['image_number'], rec['source_sha256'])] = rec
    pending = [row for row in rows if (row['property_key'], row['image_number'], row['source_sha256']) not in recovered]
    cache = Path(args.cache)
    with checkpoint.open('a', encoding='utf-8') as log, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, result in enumerate(pool.map(lambda row: scan_one(row, cache, template, args.threshold), pending), 1):
            result['_algorithm'] = identity
            recovered[(result['property_key'], result['image_number'], result['source_sha256'])] = result
            log.write(json.dumps(result) + '\n')
            log.flush()
            if i % 100 == 0:
                print(f'Scanned {i}/{len(pending)}', flush=True)
    ordered = [recovered[(r['property_key'], r['image_number'], r['source_sha256'])] for r in rows]
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    writer.writerows(ordered)
    atomic_write(output / 'results.csv', buffer.getvalue().encode())
    counts = {status: sum(r['match_status'] == status for r in ordered)
              for status in ('known_logo_template_candidate', 'no_template_match', 'unavailable', 'error')}
    summary = {'images': len(ordered), 'counts': counts, 'threshold': args.threshold,
               'reference_sha256': config['reference_sha256'], 'template_sha256': config['template_sha256'],
               'scales': SCALES, 'search_region': 'bottom 35%',
               'interpretation': 'Candidates only; no_template_match does not establish absence of a watermark.',
               'pilot_per_source': args.pilot_per_source}
    write_json(output / 'summary.json', summary)
    print(json.dumps(summary), flush=True)
    return 1 if counts['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
