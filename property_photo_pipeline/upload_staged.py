#!/usr/bin/env python3
"""Audit and optionally upload provisional, unverified property photos to R2."""
import argparse
import csv
import io
import json
import os
import re
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

from PIL import Image
from pipeline import CACHE_CONTROL, atomic_write, group, r2_settings, sha, write_json

MAX_BYTES = 400000
FIELDS = ['property_key', 'image_number', 'image_url', 'source_sha256', 'optimized_sha256',
          'optimized_bytes', 'dimensions', 'ocr_status', 'watermark_status', 'stage_status',
          'key', 'expected_public_url', 'uploaded_url', 'upload_status', 'error']


def metadata(row):
    return {'sha256': row['optimized_sha256'], 'source-sha256': row['source_sha256'],
            'watermark-status': 'unverified', 'ocr-status': row['ocr_status']}


def load_inventory(stage, cache, public_base, workers=4):
    """Audit in bounded parallelism, preserving input order and rejecting numeric aliases."""
    if not 1 <= workers <= 4:
        raise ValueError('workers must be 1–4')
    stage, cache = Path(stage).resolve(), Path(cache).resolve()
    with (stage / 'staging_inventory.csv').open(newline='', encoding='utf-8-sig') as f:
        input_rows = list(csv.DictReader(f))
    seen, duplicates = set(), []
    for input_row in input_rows:
        try:
            key = (input_row.get('property_key', ''), int(input_row.get('image_number', '')))
            duplicate = key in seen
            seen.add(key)
        except (TypeError, ValueError):
            duplicate = False
        duplicates.append(duplicate)

    def process(item):
        input_row, duplicate = item
        row = {key: input_row.get(key, '') for key in FIELDS}
        row.update(watermark_status='unverified', upload_status='unavailable', error='',
                   key='', expected_public_url='', uploaded_url='', dimensions='')
        try:
            source, ident = group(row['property_key'])
            number = int(row['image_number'])
            if number <= 0 or duplicate:
                raise ValueError('invalid or duplicate property/image number')
            if row['stage_status'] != 'encoded':
                if row['stage_status'] != 'unavailable':
                    row['upload_status'] = 'error'
                row['error'] = input_row.get('error', '') or 'staged image unavailable'
                return row
            record_path = stage / 'records' / row['property_key'] / f'{number:03d}.json'
            record = json.loads(record_path.read_text())
            for field in ('property_key', 'image_url', 'source_sha256', 'optimized_sha256', 'ocr_status', 'stage_status'):
                if str(record.get(field, '')) != str(input_row.get(field, '')):
                    raise ValueError('record and inventory mismatch: ' + field)
            if int(record['image_number']) != number:
                raise ValueError('record and inventory mismatch: image_number')
            for field in ('source_sha256', 'optimized_sha256'):
                if not re.fullmatch(r'[0-9a-f]{64}', row[field]):
                    raise ValueError('invalid ' + field)
            if not re.fullmatch(r'[a-z0-9_-]{1,100}', row['ocr_status']):
                raise ValueError('invalid OCR status')
            source_path = cache / (sha(row['image_url'].encode())[:24] + '.image')
            original = source_path.read_bytes()
            if sha(original) != row['source_sha256'] or len(original) != int(record['original_bytes']):
                raise ValueError('original bytes do not match record')
            candidate = (stage / record['staged_path']).resolve()
            if not candidate.is_relative_to(stage):
                raise ValueError('staged path escapes staging directory')
            data = candidate.read_bytes()
            if (sha(data) != row['optimized_sha256'] or len(data) != int(record['optimized_bytes'])
                    or len(data) != int(input_row['optimized_bytes']) or len(data) > MAX_BYTES):
                raise ValueError('optimized hash or byte count mismatch or exceeds 400000 bytes')
            with Image.open(io.BytesIO(data)) as image:
                dims = list(image.size)
                if image.format != 'WEBP' or dims != record['dimensions']:
                    raise ValueError('optimized format or dimensions mismatch')
            original_dims = record['original_oriented_dimensions']
            with Image.open(io.BytesIO(original)) as original_image:
                actual_original_dims = list(original_image.size)
                if original_image.getexif().get(274) in (5, 6, 7, 8):
                    actual_original_dims.reverse()
            if original_dims != actual_original_dims or len(original_dims) != 2 or any(a > b or a <= 0 for a, b in zip(dims, original_dims)):
                raise ValueError('optimized dimensions exceed original')
            row['dimensions'] = f'{dims[0]}x{dims[1]}'
            row['optimized_bytes'] = len(data)
            row['key'] = f'properties/{source}/{ident}/source/{number:03d}-{row["optimized_sha256"]}.webp'
            row['expected_public_url'] = (public_base.rstrip('/') + '/' + quote(row['key'], safe='/')) if public_base else ''
            row['upload_status'] = 'planned'
            row['_path'] = str(candidate)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            row.update(upload_status='error', error=str(exc))

        return row

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, row in enumerate(pool.map(process, zip(input_rows, duplicates)), 1):
            rows.append(row)
            if i % 1000 == 0 or i == len(input_rows):
                print(f'Audit: {i}/{len(input_rows)} images', flush=True)
    return rows


def export(rows, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    text = io.StringIO(newline='')
    writer = csv.DictWriter(text, fieldnames=FIELDS, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(output / 'inventory.csv', text.getvalue().encode())
    properties = defaultdict(list)
    for row in rows:
        properties[row['property_key']].append({k: row.get(k, '') for k in FIELDS})
    for key, entries in properties.items():
        try:
            source, ident = group(key)
        except ValueError:
            continue
        write_json(output / 'by_property' / source / ident / 'inventory.json',
                   {'property_key': key, 'entries': entries})
    counts = {status: sum(r['upload_status'] == status for r in rows)
              for status in ('planned', 'unavailable', 'error', 'uploaded', 'existing')}
    write_json(output / 'summary.json', {'images': len(rows), 'status_counts': counts,
               'watermark_status': 'unverified', 'bytes_planned': sum(int(r['optimized_bytes']) for r in rows if r['upload_status'] in ('planned', 'uploaded', 'existing'))})
    return counts


def is_missing(exc):
    return getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound')


def retry(func, attempts=4):
    for attempt in range(attempts):
        try:
            return func()
        except Exception as exc:
            code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
            if is_missing(exc) or (code and code not in ('SlowDown', 'InternalError', 'ServiceUnavailable', 'RequestTimeout', '500', '502', '503', '504')) or attempt == attempts - 1:
                raise
            time.sleep(min(2 ** attempt, 4))


def inspect_head(client, bucket, row):
    try:
        head = retry(lambda: client.head_object(Bucket=bucket, Key=row['key']))
    except Exception as exc:
        if is_missing(exc):
            return False
        raise
    # OCR can change when recognition/rules improve; immutable image identity cannot.
    identity = {k: v for k, v in metadata(row).items() if k != 'ocr-status'}
    if head.get('ContentLength') != int(row['optimized_bytes']) or any(
            head.get('Metadata', {}).get(k) != v for k, v in identity.items()):
        raise ValueError('existing object conflicts with expected bytes or metadata')
    return True


def perform_uploads(rows, client, bucket, budget, workers, checkpoint):
    """List for budget and existence; verify listed keys with HEAD before reuse."""
    if budget < 0:
        raise ValueError('budget must be nonnegative')
    used = 0
    listed_keys = set()
    pages = client.get_paginator('list_objects_v2').paginate(Bucket=bucket)
    for page in pages:
        for item in page.get('Contents', []):
            used += item['Size']
            listed_keys.add(item['Key'])
            if len(listed_keys) % 1000 == 0:
                print(f'Preflight: listed {len(listed_keys)} bucket objects', flush=True)
    planned = [r for r in rows if r['upload_status'] == 'planned']
    existing = []
    for i, row in enumerate(planned, 1):
        row['_exists'] = False
        if row['key'] in listed_keys:
            existing.append(row)
        if i % 1000 == 0 or i == len(planned):
            print(f'Preflight: indexed {i}/{len(planned)} planned images', flush=True)
    print(f'Preflight: {len(listed_keys)} bucket objects, {len(existing)} matching keys to verify', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        heads = {pool.submit(inspect_head, client, bucket, row): row for row in existing}
        for i, future in enumerate(as_completed(heads), 1):
            row = heads[future]
            try:
                row['_exists'] = future.result()
            except Exception as exc:
                row.update(upload_status='error', error='HEAD failed or object conflict: ' + type(exc).__name__)
                for pending in heads:
                    pending.cancel()
                break
            if i % 1000 == 0 or i == len(existing):
                print(f'Preflight: verified {i}/{len(existing)} existing keys', flush=True)
    if any(r['upload_status'] == 'error' for r in rows):
        raise ValueError('preflight failed; no writes attempted')
    delta = sum(int(r['optimized_bytes']) for r in planned if not r['_exists'])
    if used + delta > budget:
        raise ValueError(f'bucket budget exceeded: {used} existing + {delta} planned > {budget} bytes')
    print(f'Bucket budget: {used} existing + {delta} planned <= {budget} bytes', flush=True)
    checkpoint = Path(checkpoint)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)

    def worker(row):
        if row['_exists']:
            return 'existing'
        data = Path(row['_path']).read_bytes()
        if sha(data) != row['optimized_sha256'] or len(data) != int(row['optimized_bytes']) or len(data) > MAX_BYTES:
            raise ValueError('staged bytes changed after audit')
        try:
            retry(lambda: client.put_object(Bucket=bucket, Key=row['key'], Body=data,
                  ContentType='image/webp', CacheControl=CACHE_CONTROL, Metadata=metadata(row), IfNoneMatch='*'))
        except Exception:
            # Conditional-write races or a lost response may have left the exact object in R2.
            if inspect_head(client, bucket, row):
                return 'existing'
            raise
        return 'uploaded'

    with checkpoint.open('a', encoding='utf-8') as log, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, row): row for row in planned}
        for i, future in enumerate(as_completed(futures), 1):
            row = futures[future]
            try:
                row['upload_status'] = future.result()
                row['uploaded_url'] = row['expected_public_url']
            except Exception as exc:
                row.update(upload_status='error', error='upload failed: ' + type(exc).__name__)
            log.write(json.dumps({k: row.get(k, '') for k in FIELDS}) + '\n')
            log.flush()
            if i % 1000 == 0 or i == len(planned):
                print(f'Upload: completed {i}/{len(planned)} images', flush=True)
    return used, delta


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', default='property_photo_pipeline/staging')
    p.add_argument('--cache', default='watermark_detection/cache')
    p.add_argument('--output', default='property_photo_pipeline/staging/r2_inventory')
    p.add_argument('--execute', action='store_true')
    p.add_argument('--workers', type=int, default=16, help='Concurrent R2 operations; local file audit remains limited to four workers')
    p.add_argument('--budget-bytes', type=int, default=8_000_000_000)
    p.add_argument('--public-base-url', default=None)
    args = p.parse_args()
    if not 1 <= args.workers <= 32:
        p.error('workers must be 1–32')
    settings = r2_settings()
    public_base = settings['public_base_url'] if args.public_base_url is None else args.public_base_url
    rows = load_inventory(args.stage, args.cache, public_base, min(args.workers, 4))
    export(rows, args.output)
    if any(row['upload_status'] == 'error' for row in rows):
        print('Audit errors; no upload attempted. See inventory.')
        return 1
    if not args.execute:
        print('Dry run; no upload attempted. ' + json.dumps(export(rows, args.output)))
        return 0
    if not os.environ.get('R2_ACCESS_KEY_ID') or not os.environ.get('R2_SECRET_ACCESS_KEY'):
        print('R2 credentials are missing; no upload attempted.')
        return 1
    try:
        import boto3
        from botocore.config import Config
        client = boto3.client('s3', endpoint_url=settings['endpoint_url'], region_name='auto',
                              aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                              aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
                              config=Config(max_pool_connections=args.workers))
        perform_uploads(rows, client, settings['bucket_name'], args.budget_bytes, args.workers,
                        Path(args.output) / 'checkpoint.jsonl')
    except Exception as exc:
        print('Upload stopped: ' + type(exc).__name__ + (': ' + str(exc) if isinstance(exc, ValueError) else ''))
        export(rows, args.output)
        return 1
    counts = export(rows, args.output)
    print(json.dumps(counts))
    return 1 if counts['error'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
