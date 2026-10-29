#!/usr/bin/env python3
"""Pre-encode captured photos locally without clearing or publishing them."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
import io
import json
from pathlib import Path
import time

from pipeline import atomic_write, group, optimize, sha, write_json

SETTINGS = {'target_bytes': 300000, 'max_bytes': 400000, 'quality_floor': 75}


def stage(row, cache, output):
    """Use the captured bytes that OCR examined; never infer watermark clearance."""
    source, ident = group(row['property_key'])
    number = int(row['image_number'])
    if number < 1:
        raise ValueError('image_number must be positive')
    image_id = sha(row['image_url'].encode())[:24]
    record = {k: row.get(k, '') for k in ('property_key', 'image_number', 'image_url', 'source_sha256')}
    record.update(ocr_status=row.get('status', 'unknown'), publishable=False,
                  clearance_status='pending_verified_policy_or_removal_qc', settings=SETTINGS)
    record_path = output / 'records' / row['property_key'] / f'{number:03d}.json'
    digest = row.get('source_sha256', '')
    if not digest:
        record.update(stage_status='unavailable', error=row.get('error', 'No captured source hash'))
        write_json(record_path, record)
        return record
    raw = (cache / (image_id + '.image')).read_bytes()
    if sha(raw) != digest:
        raise ValueError('Captured source changed since OCR')
    key = f'unverified/properties/{source}/{ident}/{number:03d}-{digest}.webp'
    dest = output / key
    if record_path.exists() and dest.exists():
        old = json.loads(record_path.read_text())
        if (old.get('source_sha256') == digest and old.get('settings') == SETTINGS
                and old.get('stage_status') == 'encoded' and sha(dest.read_bytes()) == old.get('optimized_sha256')):
            old.update(ocr_status=record['ocr_status'], publishable=False,
                       clearance_status=record['clearance_status'])
            write_json(record_path, old)
            return old
    encoded, size, original_size, quality = optimize(raw, SETTINGS['target_bytes'], SETTINGS['max_bytes'], SETTINGS['quality_floor'])
    atomic_write(dest, encoded)
    record.update(stage_status='encoded', original_bytes=len(raw), optimized_bytes=len(encoded),
                  original_oriented_dimensions=list(original_size), dimensions=list(size), quality=quality,
                  optimized_sha256=sha(encoded), staged_path=key)
    write_json(record_path, record)
    return record


def export(output, records, finished, final=False):
    rows = sorted(records.values(), key=lambda r: (r['property_key'], int(r['image_number'])))
    summary = {'checkpoint_rows_processed': len(rows), 'ocr_scan_finished': finished,
               'stage_statuses': dict(Counter(r['stage_status'] for r in rows)),
               'optimized_bytes': sum(r.get('optimized_bytes', 0) for r in rows),
               'publishable_images': 0, 'uploaded_images': 0, 'image_editing_calls': 0,
               'purpose': 'Private provisional encodings; watermarks are unchanged and no image is cleared.'}
    write_json(output / 'progress.json', summary)
    if finished or final:
        fields = ['property_key', 'image_number', 'image_url', 'source_sha256', 'ocr_status',
                  'stage_status', 'optimized_bytes', 'optimized_sha256', 'staged_path', 'publishable', 'error']
        text = io.StringIO(newline='')
        writer = csv.DictWriter(text, fieldnames=fields, extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
        atomic_write(output / 'staging_inventory.csv', text.getvalue().encode())
        write_json(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr-run', default='watermark_detection/runs/ocr_full')
    parser.add_argument('--cache', default='watermark_detection/cache')
    parser.add_argument('--output', default='property_photo_pipeline/staging')
    parser.add_argument('--workers', type=int, default=2)
    parser.add_argument('--watch', action='store_true', help='Follow OCR until its final summary exists')
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        parser.error('workers must be between 1 and 4')
    run, cache, output = Path(args.ocr_run), Path(args.cache), Path(args.output)
    checkpoint = run / 'checkpoint.jsonl'
    if not checkpoint.exists():
        parser.error('Start OCR first; checkpoint is missing')
    output.mkdir(parents=True, exist_ok=True)
    results, pending, submitted = {}, {}, set()
    with checkpoint.open() as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
        at_end = False
        while True:
            while len(pending) < args.workers * 2:
                position = stream.tell(); line = stream.readline()
                if not line or not line.endswith('\n'):
                    stream.seek(position); at_end = True; break
                at_end = False
                row = json.loads(line)
                key = (row['property_key'], str(row['image_number']))
                identity = key + (row.get('source_sha256', ''), row.get('status', ''))
                if identity in submitted:
                    continue
                submitted.add(identity)
                pending[pool.submit(stage, row, cache, output)] = (key, row)
            if pending:
                done, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    key, row = pending.pop(future)
                    try:
                        results[key] = future.result()
                    except Exception as exc:
                        results[key] = {k: row.get(k, '') for k in ('property_key', 'image_number', 'image_url', 'source_sha256')}
                        results[key].update(stage_status='error', error=str(exc), publishable=False)
                    if len(results) % 25 == 0:
                        print(json.dumps(export(output, results, False)), flush=True)
            if at_end and not pending:
                if (run / 'summary.json').exists() or not args.watch:
                    print(json.dumps(export(output, results, (run / 'summary.json').exists(), final=True)), flush=True)
                    break
                export(output, results, False)
                time.sleep(2)


if __name__ == '__main__':
    main()
