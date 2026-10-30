#!/usr/bin/env python3
"""Publish a verified upload inventory and versioned property photo catalog."""
import argparse
import csv
import json
import os
from collections import defaultdict
from pathlib import Path

from pipeline import r2_settings, sha, write_json
from build_triage import FIELDS as REPORT_FIELDS

OWNER = 'estScra-property-photo-import'
PREFIX = 'graden-imports/property-photos'


def build_catalog(reports):
    reports = Path(reports)
    summary = json.loads((reports/'summary.json').read_text())
    with (reports/'all_images.csv').open(newline='') as file:
        reader = csv.DictReader(file)
        if set(reader.fieldnames or []) != set(REPORT_FIELDS):
            raise ValueError('Unexpected report schema; refusing to publish internal or incomplete fields')
        rows = list(reader)
    properties = defaultdict(lambda: {'images': [], 'unavailable': []})
    seen = set()
    for row in rows:
        key = (row['property_key'], row['image_number'])
        if key in seen:
            raise ValueError('Duplicate property image')
        seen.add(key)
        prop = properties[row['property_key']]
        prop.update(property_key=row['property_key'], listing_url=row['listing_url'])
        if row['upload_status'] == 'unavailable':
            prop['unavailable'].append({'image_number': int(row['image_number']), 'original_url': row['image_url']})
            continue
        if row['upload_status'] not in ('uploaded', 'existing') or not row['uploaded_url']:
            raise ValueError('Every available photo must have a confirmed upload before catalog publication')
        prop['images'].append({
            'image_number': int(row['image_number']), 'active_url': row['uploaded_url'],
            'source_copy_url': row['uploaded_url'], 'cleaned_url': None, 'selected_version': 'source',
            'watermark_status': 'unverified', 'watermark_candidate': row['candidate_method'] != 'none',
            'detection_methods': row['candidate_method'], 'detection_group': row['triage_group'],
            'pattern_id': row['pattern_id'], 'source_sha256': row['source_sha256'],
            'optimized_sha256': row['optimized_sha256'], 'bytes': int(row['optimized_bytes'])})
    catalog = {'schema_version': 1, 'created_at': summary['created_at'],
               'available_images': sum(len(p['images']) for p in properties.values()),
               'unavailable_images': sum(len(p['unavailable']) for p in properties.values()),
               'watermark_candidates': summary['combined_candidate_images'],
               'note': 'Source copies retain existing watermarks. Negative detection is not a clean-image certificate.',
               'properties': {k: dict(properties[k]) for k in sorted(properties)}}
    return catalog


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'\n').encode()


def existing(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return None
        raise


def publish(client, bucket, public_base, reports, output, budget=8_000_000_000):
    catalog = build_catalog(reports)
    if not public_base.startswith('https://') or not all(
            item['active_url'].startswith(public_base.rstrip('/')+'/')
            for prop in catalog['properties'].values() for item in prop['images']):
        raise ValueError('Confirmed image URLs do not match the configured public delivery base')
    main = encode(catalog)
    run_prefix = PREFIX+'/'+sha(main)[:24]
    files = {'properties.json': (main, 'application/json')}
    for name, mime in [('all_images.csv','text/csv; charset=utf-8'),
                       ('watermark_candidates.csv','text/csv; charset=utf-8'),
                       ('per_property_triage.zip','application/zip'), ('summary.json','application/json')]:
        files[name] = ((Path(reports)/name).read_bytes(), mime)
    urls = {name: public_base.rstrip('/')+'/'+run_prefix+'/'+name for name in files}
    pointer = encode({'schema_version': 1, 'catalog_url': urls['properties.json'], 'assets': urls,
                      'available_images': catalog['available_images'], 'watermark_candidates': catalog['watermark_candidates']})
    pointer_key = PREFIX+'/latest.json'
    heads = {name: existing(client, bucket, run_prefix+'/'+name) for name in files}
    for name, head in heads.items():
        if head and (head.get('Metadata', {}).get('sha256') != sha(files[name][0]) or head['ContentLength'] != len(files[name][0])):
            raise ValueError('Immutable catalog object conflicts')
    old_pointer = existing(client, bucket, pointer_key)
    if old_pointer and old_pointer.get('Metadata', {}).get('owner') != OWNER:
        raise ValueError('Existing catalog pointer is not owned by this importer')
    used = sum(item['Size'] for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket) for item in page.get('Contents', []))
    delta = sum(len(data) for name, (data, _) in files.items() if heads[name] is None)
    delta += max(0, len(pointer)-(old_pointer or {}).get('ContentLength', 0))
    if used+delta > budget:
        raise ValueError('Bucket budget would be exceeded by catalog assets')
    for name, (data, mime) in files.items():
        if heads[name] is None:
            download = {'ContentDisposition': 'attachment; filename="'+name+'"'} if mime != 'application/json' else {}
            client.put_object(Bucket=bucket, Key=run_prefix+'/'+name, Body=data, ContentType=mime,
                              CacheControl='public, max-age=31536000, immutable',
                              Metadata={'owner': OWNER, 'sha256': sha(data)}, IfNoneMatch='*', **download)
    condition = {'IfMatch': old_pointer['ETag']} if old_pointer else {'IfNoneMatch': '*'}
    client.put_object(Bucket=bucket, Key=pointer_key, Body=pointer, ContentType='application/json',
                      CacheControl='public, max-age=60', Metadata={'owner': OWNER, 'sha256': sha(pointer)}, **condition)
    result = {'status': 'published', 'latest_url': public_base.rstrip('/')+'/'+pointer_key,
              'assets': urls, 'available_images': catalog['available_images'],
              'watermark_candidates': catalog['watermark_candidates'], 'catalog_bytes': len(main)}
    write_json(Path(output)/'publication.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', default='property_photo_pipeline/reports')
    parser.add_argument('--output', default='property_photo_pipeline/staging/r2_catalog')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        catalog = build_catalog(args.reports)
        write_json(Path(args.output)/'properties.json', catalog)
        print('Prepared confirmed-upload catalog locally; no network writes.')
        return
    import boto3
    settings = r2_settings()
    client = boto3.client('s3', endpoint_url=settings['endpoint_url'], region_name='auto',
                          aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'])
    print(json.dumps(publish(client, settings['bucket_name'], settings['public_base_url'], args.reports, args.output), indent=2))


if __name__ == '__main__':
    main()
