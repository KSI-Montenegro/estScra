#!/usr/bin/env python3
"""Run the resumable local cleanup and publish a small, public progress record."""
from collections import Counter
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

from pipeline import r2_settings, sha, write_json

OWNER = 'estScra-property-photo-import'
KEY = 'graden-imports/property-photos/removal-progress.json'


def snapshot(folder, began, returncode=None):
    records = {}
    checkpoint = folder/'checkpoint.jsonl'
    if checkpoint.exists():
        for line in checkpoint.open():
            try: row = json.loads(line)
            except ValueError: continue
            records[(row['property_key'],str(row['image_number']),row['source_sha256'])] = row
    counts = Counter(r['status'] for r in records.values())
    uploaded = sum(r.get('upload_status') in ('uploaded','existing') for r in records.values())
    total = 4789; elapsed = time.monotonic()-began
    phase = 'processing' if records else 'starting'
    if len(records) >= total:
        phase = 'uploading_cleaned_images' if uploaded < counts['processed'] else 'publishing_catalog'
    if returncode is not None:
        phase = 'completed_with_exceptions' if returncode == 0 and uploaded < total else 'completed' if returncode == 0 else 'failed'
    eta = round(max(0,total-len(records))*elapsed/len(records)) if records else None
    return {'updated_at':datetime.now(timezone.utc).isoformat(),'phase':phase,'candidate_images':total,
            'checked':len(records),'locally_processed':counts['processed'],'queued':counts['queued'],
            'processing_errors':counts['error'],'cleaned_images_uploaded':uploaded,
            'estimated_processing_seconds_remaining':eta,'elapsed_seconds':round(elapsed),
            'catalog_index':'https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/property-photos/latest.json',
            'method':'Local LaMa CPU; no per-image editing API fee',
            'note':'Accepted outputs use masked reconstruction and automated checks; queued/failed images retain source copies.'}


def publish_progress(client, bucket, data):
    body = (json.dumps(data,sort_keys=True)+'\n').encode()
    try:
        old = client.head_object(Bucket=bucket,Key=KEY)
    except Exception as exc:
        if getattr(exc,'response',{}).get('Error',{}).get('Code') not in ('404','NoSuchKey','NotFound'): raise
        old = None
    if old and old.get('Metadata',{}).get('owner') != OWNER:
        raise ValueError('Existing progress object is not owned by this importer')
    condition = {'IfMatch':old['ETag']} if old else {'IfNoneMatch':'*'}
    client.put_object(Bucket=bucket,Key=KEY,Body=body,ContentType='application/json',CacheControl='no-store',
                      Metadata={'owner':OWNER,'sha256':sha(body)},**condition)


def main():
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)
    folder = root/'property_photo_pipeline/staging/local_removal';folder.mkdir(parents=True,exist_ok=True)
    with (folder/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        import boto3
        settings = r2_settings()
        client = boto3.client('s3',endpoint_url=settings['endpoint_url'],region_name='auto',
                              aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'])
        began = time.monotonic()
        initial = snapshot(folder,began);publish_progress(client,settings['bucket_name'],initial)
        with (folder/'pipeline.log').open('a') as log:
            process = subprocess.Popen(['bash','property_photo_pipeline/clean_images.sh'],stdout=log,stderr=subprocess.STDOUT)
            while True:
                returncode = process.poll()
                progress = snapshot(folder,began,returncode)
                write_json(folder/'public_progress.json',progress)
                try: publish_progress(client,settings['bucket_name'],progress)
                except Exception as exc: print('Progress publication error: '+type(exc).__name__,flush=True)
                print(json.dumps(progress),flush=True)
                if returncode is not None: break
                time.sleep(30)
        if returncode: raise SystemExit(returncode)


if __name__=='__main__': main()
