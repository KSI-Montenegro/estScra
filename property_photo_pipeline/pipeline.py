#!/usr/bin/env python3
"""Prepare explicitly cleared local originals and optionally publish to R2."""
import argparse
import csv
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

from PIL import Image, ImageOps

CACHE_CONTROL = 'public, max-age=31536000, immutable'


def r2_settings():
    config = json.loads(Path(__file__).with_name('r2_config.json').read_text())
    return {key: os.environ.get(env) or config[key] for key, env in (
        ('endpoint_url', 'R2_ENDPOINT_URL'), ('bucket_name', 'R2_BUCKET_NAME'),
        ('public_base_url', 'R2_PUBLIC_BASE_URL'))}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def csv_rows(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def clearances(path):
    result = {}
    for row in csv_rows(path):
        digest = row.get('original_sha256', '').strip().lower()
        decision = row.get('decision', '').strip().lower()
        source = row.get('decision_source', '').strip()
        if not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid clearance SHA256')
        if digest in result:
            raise ValueError('Duplicate clearance for SHA256 ' + digest)
        result[digest] = (decision, source)
    return result


def require_clearance(digest, decisions):
    decision, source = decisions.get(digest, ('', ''))
    if decision != 'cleared' or not source or ('no_text' in source.lower() or source.lower().startswith('ocr')):
        raise ValueError('Explicit clearance and decision source required for ' + digest)
    return source


def group(key):
    match = re.fullmatch(r'([a-zA-Z0-9-]+)_([a-zA-Z0-9-]+)', key)
    if not match:
        raise ValueError('Invalid property_key: ' + key)
    return match.group(1), match.group(2)


def optimize(data, target, maximum, quality_floor):
    if not (0 < target <= maximum and 1 <= quality_floor <= 95):
        raise ValueError('Require 0 < target <= maximum and 1 <= quality floor <= 95')
    with Image.open(io.BytesIO(data)) as im:
        im = ImageOps.exif_transpose(im)
        im.load()
        if im.mode not in ('RGB', 'RGBA'):
            im = im.convert('RGBA' if 'transparency' in im.info else 'RGB')
        original_size = im.size
        while True:
            best = None
            # Try each quality because compressed sizes are not guaranteed monotonic.
            for q in range(95, quality_floor - 1, -1):
                output = io.BytesIO()
                im.save(output, format='WEBP', quality=q, method=6)
                encoded = output.getvalue()
                if len(encoded) <= target:
                    return encoded, im.size, original_size, q
                if len(encoded) <= maximum and best is None:
                    best = (encoded, im.size, original_size, q)
            # Preserve dimensions whenever the hard limit can be met.
            if best:
                return best
            if min(im.size) <= 64:
                raise ValueError('Cannot meet maximum size at the quality floor')
            size = (max(1, int(im.width * .85)), max(1, int(im.height * .85)))
            if size == im.size:
                raise ValueError('Cannot resize image further')
            im = im.resize(size, Image.Resampling.LANCZOS)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_json(path, data):
    atomic_write(path, (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode())


def prepare(args):
    decisions = clearances(args.clearances)
    manifest_rows = csv_rows(args.manifest)
    cache_dir = getattr(args, 'cache_dir', None)
    local_rows = csv_rows(args.local_files) if not cache_dir else []
    local = {}
    for row in local_rows:
        url = row.get('image_url', '').strip()
        if not url or not row.get('local_path', '').strip() or url in local:
            raise ValueError('Local files CSV requires unique image_url and local_path')
        path = Path(row['local_path'])
        local[url] = (Path(args.local_files).resolve().parent / path).resolve()
    out = Path(args.output).resolve()
    entries, errors, seen = [], [], {}
    # Stable order chooses the lowest image number when a property repeats bytes.
    def sort_key(row):
        try:
            return row['property_key'], int(row['image_number']), row['image_url']
        except (KeyError, ValueError):
            return row.get('property_key', ''), 0, row.get('image_url', '')
    for row in sorted(manifest_rows, key=sort_key):
        try:
            source, ident = group(row['property_key'])
            number = int(row['image_number'])
            if number < 1:
                raise ValueError('image_number must be positive')
            url = row['image_url']
            if cache_dir:
                path = (Path(cache_dir).resolve() / (sha(url.encode('utf-8'))[:24] + '.image'))
            else:
                if url not in local:
                    raise ValueError('No local original mapped for image URL')
                path = local[url]
            original = path.read_bytes()
            digest = sha(original)
            decision_source = require_clearance(digest, decisions)
            duplicate = (source, ident, digest)
            if duplicate in seen:
                seen[duplicate]['duplicate_image_urls'].append(url)
                continue
            encoded, size, original_size, quality = optimize(original, args.target_bytes, args.max_bytes, args.quality_floor)
            key = f'properties/{source}/{ident}/{number:03d}-{digest}.webp'
            dest = out / key
            atomic_write(dest, encoded)
            entry = {'property_key': row['property_key'], 'listing_url': row.get('listing_url', ''),
                     'image_number': number, 'image_url': url, 'duplicate_image_urls': [],
                     'original_path': str(path), 'acquisition': 'url_sha256_cache' if cache_dir else 'local_mapping', 'original_sha256': digest, 'original_bytes': len(original),
                     'original_oriented_dimensions': list(original_size), 'decision_source': decision_source,
                     'key': key, 'ready_path': str(dest), 'sha256': sha(encoded), 'bytes': len(encoded),
                     'dimensions': list(size), 'quality': quality, 'max_bytes': args.max_bytes}
            entries.append(entry)
            seen[duplicate] = entry
        except (ValueError, OSError, KeyError) as exc:
            errors.append({'property_key': row.get('property_key', ''), 'image_number': row.get('image_number', ''),
                           'image_url': row.get('image_url', ''), 'error': str(exc)})
    result = {'version': 1, 'max_bytes': args.max_bytes, 'entries': entries, 'errors': errors}
    write_json(out / 'manifest.json', result)
    for property_key in sorted({e['property_key'] for e in entries}):
        source, ident = group(property_key)
        write_json(out / 'properties' / source / ident / 'manifest.json',
                   {'version': 1, 'property_key': property_key,
                    'entries': [e for e in entries if e['property_key'] == property_key]})
    write_json(out / 'summary.json', {'input_rows': len(manifest_rows), 'ready': len(entries),
                                     'duplicates': sum(len(e['duplicate_image_urls']) for e in entries),
                                     'errors': len(errors), 'error_details': errors})
    print(f"Prepared {len(entries)} images; {len(errors)} rows skipped. See {out / 'summary.json'}")
    return 1 if errors else 0


def audit(manifest, decisions):
    entries = json.loads(Path(manifest).read_text(encoding='utf-8'))['entries']
    for entry in entries:
        source, ident = group(entry['property_key'])
        expected_key = f"properties/{source}/{ident}/{int(entry['image_number']):03d}-{entry['original_sha256']}.webp"
        if entry['key'] != expected_key:
            raise ValueError('Invalid object key')
        original = Path(entry['original_path']).read_bytes()
        if sha(original) != entry['original_sha256'] or len(original) != entry['original_bytes']:
            raise ValueError('Original bytes changed: ' + entry['original_path'])
        if require_clearance(entry['original_sha256'], decisions) != entry['decision_source']:
            raise ValueError('Clearance source changed for ' + entry['original_sha256'])
        ready = Path(entry['ready_path']).read_bytes()
        if entry['max_bytes'] <= 0 or len(ready) > entry['max_bytes']:
            raise ValueError('Ready image exceeds recorded maximum')
        if any(a > b for a, b in zip(entry['dimensions'], entry['original_oriented_dimensions'])):
            raise ValueError('Ready image exceeds original dimensions')
        if sha(ready) != entry['sha256'] or len(ready) != entry['bytes']:
            raise ValueError('Ready bytes changed: ' + entry['ready_path'])
        with Image.open(io.BytesIO(ready)) as im:
            if im.format != 'WEBP' or list(im.size) != entry['dimensions']:
                raise ValueError('Ready format or dimensions changed')
    return entries


def upload(args):
    entries = audit(args.manifest, clearances(args.clearances))
    if not args.execute:
        print(f'Audited {len(entries)} images; dry run, no upload.')
        return 0
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    names = ['R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY']
    if not all(os.environ.get(n) for n in names):
        raise ValueError('Set all required R2 environment variables')
    settings = r2_settings()
    public_base_url = args.public_base_url if args.public_base_url is not None else settings['public_base_url']
    client = boto3.client('s3', endpoint_url=settings['endpoint_url'],
                          aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                          aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'], region_name='auto')
    bucket = settings['bucket_name']
    if args.budget_bytes < 0:
        raise ValueError('Budget must be nonnegative')
    used = 0
    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        used += sum(item['Size'] for item in page.get('Contents', []))
    planned = 0
    for entry in entries:
        try:
            head = client.head_object(Bucket=bucket, Key=entry['key'])
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                raise
            planned += entry['bytes']
            continue
        if head.get('Metadata', {}).get('sha256') != entry['sha256'] or head.get('ContentLength') != entry['bytes']:
            raise ValueError('Existing object differs: ' + entry['key'])
    if used + planned > args.budget_bytes:
        raise ValueError(f'Bucket budget exceeded: existing {used} + planned {planned} > {args.budget_bytes} bytes')
    print(f'Bucket budget: {used} existing + {planned} planned <= {args.budget_bytes} bytes')
    published = []
    for entry in entries:
        # Recheck immediately before upload as well as during the all-entry audit.
        audit_entry_original = Path(entry['original_path']).read_bytes()
        if sha(audit_entry_original) != entry['original_sha256']:
            raise ValueError('Original bytes changed before upload')
        data = Path(entry['ready_path']).read_bytes()
        if sha(data) != entry['sha256']:
            raise ValueError('Ready bytes changed before upload')
        for attempt in range(4):
            try:
                try:
                    head = client.head_object(Bucket=bucket, Key=entry['key'])
                except ClientError as exc:
                    if exc.response.get('Error', {}).get('Code') not in ('404', 'NoSuchKey', 'NotFound'):
                        raise
                    head = None
                if head is not None:
                    if head.get('Metadata', {}).get('sha256') != entry['sha256'] or head.get('ContentLength') != len(data):
                        raise ValueError('Existing object differs: ' + entry['key'])
                else:
                    client.put_object(Bucket=bucket, Key=entry['key'], Body=data, ContentType='image/webp',
                                      CacheControl=CACHE_CONTROL, Metadata={'sha256': entry['sha256']}, IfNoneMatch='*')
                break
            except (BotoCoreError, ClientError, OSError):
                if attempt == 3:
                    raise
                time.sleep(2 ** attempt)
        print('Uploaded or already present: ' + entry['key'])
        url = public_base_url.rstrip('/') + '/' + quote(entry['key'], safe='/') if public_base_url else ''
        if url:
            print(url)
        published.append({'property_key': entry['property_key'], 'image_number': entry['image_number'],
                          'key': entry['key'], 'sha256': entry['sha256'], 'bytes': entry['bytes'], 'public_url': url})
    output = Path(args.manifest).resolve().parent
    csv_data = io.StringIO(newline='')
    writer = csv.DictWriter(csv_data, fieldnames=['property_key', 'image_number', 'key', 'sha256', 'bytes', 'public_url'])
    writer.writeheader()
    writer.writerows(published)
    atomic_write(output / 'published.csv', csv_data.getvalue().encode())
    for property_key in sorted({e['property_key'] for e in published}):
        source, ident = group(property_key)
        write_json(output / 'properties' / source / ident / 'published.json',
                   {'property_key': property_key, 'entries': [e for e in published if e['property_key'] == property_key]})
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('prepare', help='Prepare locally mapped originals')
    p.add_argument('--manifest', required=True)
    inputs = p.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--local-files')
    inputs.add_argument('--cache-dir', help='Existing URL-hashed downloader cache')
    p.add_argument('--clearances', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--target-bytes', type=int, default=300000)
    p.add_argument('--max-bytes', type=int, default=400000)
    p.add_argument('--quality-floor', type=int, default=75)
    p = subs.add_parser('audit', help='Validate all ready files and original clearances')
    p.add_argument('--manifest', required=True)
    p.add_argument('--clearances', required=True)
    p = subs.add_parser('upload', help='Audit and optionally upload to R2')
    p.add_argument('--manifest', required=True)
    p.add_argument('--clearances', required=True)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--budget-bytes', type=int, default=8_000_000_000, help='Maximum total bucket bytes (default 8 GB)')
    p.add_argument('--public-base-url', default=None)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            return prepare(args)
        if args.command == 'upload':
            return upload(args)
        entries = audit(args.manifest, clearances(args.clearances))
        print(f'Audited {len(entries)} images.')
        return 0
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print('Error: ' + str(exc), file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
