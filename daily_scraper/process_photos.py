#!/usr/bin/env python3
"""Resumable one-sample-per-property detection, local LaMa cleanup and R2 delivery."""
import argparse
from collections import Counter
import copy
import csv
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import zipfile
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent if (HERE.parent / 'property_photo_pipeline').exists() else HERE
sys.path[:0] = [str(ROOT / 'property_photo_pipeline'), str(ROOT / 'watermark_detection')]
import cv2
import numpy as np
from PIL import Image, ImageOps
from pipeline import optimize, atomic_write, write_json, r2_settings
from match_known_logo import match_image
from ocr_scan import ocr, brand_hits, token
from watermark_mask import build_mask
from remove_local import process_row, TorchInpainter
from photo_catalog import R2Store, is_cleaned, csv_bytes, sha
from photo_queue import prepare, enqueue

VERSION = 'one-property-sample-patuljak-v1'
HOSTS = {'content.estitor.com', 'patuljak.me', 'www.patuljak.me', 'www.realitica.com', 'realitica.com'}
cv2.setNumThreads(1)


class PhotoFailure(Exception):
    pass


def safe_url(url, hosts):
    u = urlsplit(url)
    return u.scheme == 'https' and u.hostname in hosts and not u.username and not u.password and u.port in (None, 443)


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not safe_url(newurl, self.hosts):
            raise PhotoFailure('image_redirect_not_allowed')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Downloader:
    def __init__(self, cache, public_host, key='', zone='esstitor_site_phone_scraper', max_api=20):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.hosts = HOSTS | {public_host}
        self.opener = build_opener(SafeRedirect(self.hosts))
        self.next_request, self.paused = {}, set()
        self.key, self.zone, self.max_api, self.api_requests = key, zone, max_api, 0

    def get(self, url):
        if not safe_url(url, self.hosts):
            raise PhotoFailure('image_url_not_allowed')
        path = self.cache / (sha(url.encode())[:24] + '.image')
        if path.exists():
            return path.read_bytes()
        if shutil.disk_usage(self.cache).free < 1_000_000_000:
            raise PhotoFailure('local_disk_reserve_reached')
        host = urlsplit(url).hostname
        if host in self.paused:
            raise PhotoFailure('image_host_rate_limited')
        time.sleep(max(0, self.next_request.get(host, 0) - time.monotonic()))
        self.next_request[host] = time.monotonic() + .5
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; GRADEN-PropertyPhotos/1.0)'})
        try:
            with self.opener.open(request, timeout=25) as response:
                data = response.read(20_000_001)
        except HTTPError as exc:
            if exc.code == 429:
                self.paused.add(host)
                raise PhotoFailure('image_http_429') from None
            if exc.code != 403 or not self.key or self.api_requests >= self.max_api or host not in HOSTS:
                raise PhotoFailure('image_http_' + str(exc.code)) from None
            # Raw API mode is required for original image bytes, not JSON HTML.
            payload = json.dumps({'zone': self.zone, 'url': url, 'format': 'raw', 'method': 'GET'}).encode()
            self.api_requests += 1
            api = Request('https://api.brightdata.com/request', data=payload,
                          headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            try:
                with build_opener().open(api, timeout=45) as response:
                    data = response.read(20_000_001)
            except Exception:
                raise PhotoFailure('image_proxy_fetch_failed') from None
        except PhotoFailure:
            raise
        except Exception:
            raise PhotoFailure('image_connection_failed') from None
        if len(data) > 20_000_000:
            raise PhotoFailure('image_bytes_limit')
        decode(data)  # Reject challenges, invalid images and oversized decodes.
        atomic_write(path, data)
        return data


def decode(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise PhotoFailure('image_pixel_limit')
        return np.asarray(ImageOps.exif_transpose(image).convert('RGB'))


class Detector:
    def __init__(self, template=None):
        folder = ROOT / 'property_photo_pipeline/reference_assets'
        config = json.loads((folder / 'patuljak_mascot.json').read_text())
        data = (folder / config['template_file']).read_bytes()
        if sha(data) != config['template_sha256']:
            raise ValueError('Template digest mismatch')
        self.template = np.asarray(Image.open(io.BytesIO(data)).convert('RGB')) if template is None else template

    def analyze(self, data):
        rgb = decode(data)
        height, width = rgb.shape[:2]
        if min(width, height) < 100:
            return {'result': 'needs_review', 'reason': 'small_preview_only', 'score': 0, 'box': [], 'hits': []}
        try:
            score, box, _ = match_image(rgb, self.template)
        except ValueError:
            score, box = 0., []
        words = ocr(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), 'tesseract', 30)
        hits = brand_hits(words, width, height)
        text_found = any(w['confidence'] >= 45 and len(token(w['text'])) >= 3 for w in words)
        known = score >= .85 or bool(hits)
        return {'result': 'watermark_candidate' if known else 'needs_review' if text_found else 'sample_passed',
                'reason': 'known_logo_or_brand' if known else 'unclassified_text' if text_found else 'no_detected_mark_in_sample',
                'score': float(score), 'box': box, 'hits': hits,
                'width': width, 'height': height, 'other_text_detected': text_found}


class PhotoWorker:
    def __init__(self, state, state_path, output, store, detector, downloader, model_path,
                 execute=False, max_images=1500, max_clean=300):
        self.state, self.state_path, self.output = state, Path(state_path), Path(output)
        self.store, self.detector, self.downloader = store, detector, downloader
        self.model_path, self.execute = Path(model_path), execute
        self.max_images, self.max_clean = max_images, max_clean
        self.infer, self.model_hash = None, None
        self.counts, self.updates = Counter(), {}
        self.catalog, _ = store.snapshot() if store else ({'properties': {}}, None)

    def checkpoint(self):
        write_json(self.state_path, self.state)

    def repair(self, key, item, data, analysis):
        row = {'property_key': key, 'image_number': item['image_number'], 'image_url': item['original_url'],
               'source_sha256': sha(data), 'logo_template_score': analysis['score'],
               'logo_box_original_pixels': json.dumps(analysis['box']), 'ocr_hits_json': json.dumps(analysis['hits']),
               'triage_group': 'known_logo_template_candidate' if analysis['score'] >= .85 else 'ocr_brand_candidate',
               'candidate_method': 'template_and_ocr'}
        mask, meta = build_mask(row, decode(data))
        if not meta.get('eligible'):
            return None, 'unsupported_or_uncertain_mask'
        if not self.execute:
            return None, 'removal_execute_required'
        if self.counts['removal_attempts'] >= self.max_clean:
            return None, 'removal_run_limit'
        if self.infer is None:
            cfg = json.loads((ROOT / 'property_photo_pipeline/lama_model.json').read_text())
            self.model_hash = sha(self.model_path.read_bytes())
            if self.model_hash != cfg['sha256']:
                raise PhotoFailure('lama_model_digest_mismatch')
            self.infer = TorchInpainter(self.model_path, 2)
        self.counts['removal_attempts'] += 1
        result = process_row(row, self.downloader.cache, self.output / 'repairs', build_mask,
                             self.infer, self.detector.template, self.model_hash, VERSION, context=96)
        if result['status'] != 'processed':
            return None, 'repair_automated_checks_failed'
        self.counts['cleaned'] += 1
        return result, ''

    def process(self, prop):
        key = prop['property_key']
        current = self.catalog.get('properties', {}).get(key, {})
        existing = {str(r['image_number']): r for r in current.get('images', [])}
        if current.get('photo_pipeline_version') == VERSION and current.get('input_signature') == prop['input_signature']:
            prop['sample'] = current.get('watermark_sample', {})
            for number, row in existing.items():
                if row.get('pipeline_status') not in ('retry', 'deferred_cleanup'):
                    prop['images'][number] = {**copy.deepcopy(row), 'reused_existing': True, 'upload_confirmed': True}
        for number, row in existing.items():
            if is_cleaned(row):
                prop['images'][number] = {**copy.deepcopy(row), 'reused_existing': True,
                                          'ready_for_listing': True, 'pipeline_status': 'ready_existing_cleaned'}
        unprocessed = [i for i in prop['input_images'] if not is_cleaned(prop['images'].get(str(i['image_number']), {}))]
        if not prop['input_images']:
            prop['phase'] = 'no_images'
            self.checkpoint()
            return
        known_pending = any(r.get('watermark_candidate') and not is_cleaned(r) for r in existing.values())
        if unprocessed and not prop.get('sample'):
            first = unprocessed[0]
            sample_url = first['original_url']
            try:
                try:
                    data = self.downloader.get(sample_url)
                except Exception:
                    base = existing.get(str(first['image_number']), {})
                    sample_url = base.get('source_copy_url')
                    if not sample_url:
                        raise PhotoFailure('sample_download_failed') from None
                    data = self.downloader.get(sample_url)
                    if sha(data) != base['optimized_sha256']:
                        raise PhotoFailure('sample_source_copy_digest_mismatch')
                analysis = self.detector.analyze(data)
                prop['sample'] = {**analysis, 'image_number': first['image_number'], 'sampled_url': sample_url,
                                  'source_sha256': sha(data), 'images_sampled': 1,
                                  'meaning': 'One source image checked; remaining images are not individually certified.'}
                self.counts['properties_sampled'] += 1
            except Exception as exc:
                prop['sample'] = {'result': 'needs_review', 'reason': str(exc) if isinstance(exc, PhotoFailure) else 'sample_analysis_failed',
                                  'image_number': first['image_number'], 'sampled_url': sample_url,
                                  'images_sampled': 1}
        flagged = known_pending or prop.get('sample', {}).get('result') != 'sample_passed'
        for item in prop['input_images']:
            number = str(item['image_number'])
            done = prop['images'].get(number)
            if done and done.get('upload_confirmed') and done.get('pipeline_status') != 'deferred_cleanup':
                continue
            if done and is_cleaned(done) and done.get('reused_existing'):
                self.counts['reused_cleaned'] += 1
                continue
            base = copy.deepcopy(existing.get(number, {}))
            if base and not flagged and not base.get('watermark_candidate'):
                prop['images'][number] = {**base, 'reused_existing': True, 'original_url': item['original_url'],
                                         'ready_for_listing': True, 'pipeline_status': 'ready_sampled_source',
                                         'watermark_status': 'sample_passed', 'sample_only': True,
                                         'upload_confirmed': True, 'error_code': ''}
                self.counts['reused_source'] += 1
                self.checkpoint()
                continue
            if self.counts['images_processed'] >= self.max_images:
                break
            self.counts['images_processed'] += 1
            try:
                data = self.downloader.get(item['original_url'])
                digest = sha(data)
                if base and digest != base.get('source_sha256'):
                    prop['images'][number] = {**base, 'original_url': item['original_url'], 'reused_existing': True,
                                             'ready_for_listing': False, 'pipeline_status': 'needs_review',
                                             'error_code': 'original_changed_since_existing_import',
                                             'watermark_candidate': bool(flagged or base.get('watermark_candidate'))}
                    self.checkpoint()
                    continue
                if base:
                    row = {**base, 'reused_existing': True, 'original_url': item['original_url']}
                else:
                    encoded, dimensions, original_dimensions, quality = optimize(data, 300000, 400000, 75)
                    encoded_sha = sha(encoded)
                    source, ident = key.split('_', 1)
                    object_key = f'properties/{source}/{ident}/source/{int(number):03d}-{encoded_sha}.webp'
                    atomic_write(self.output / 'ready' / object_key, encoded)
                    source_url = self.store.put(object_key, encoded, 'image/webp', {'source-sha256': digest, 'watermark-status': 'unverified'}) if self.store else ''
                    row = {'image_number': int(number), 'original_url': item['original_url'],
                           'source_copy_url': source_url, 'active_url': source_url, 'cleaned_url': None,
                           'selected_version': 'source', 'source_sha256': digest, 'optimized_sha256': encoded_sha,
                           'bytes': len(encoded), 'dimensions': list(dimensions), 'original_dimensions': list(original_dimensions),
                           'quality': quality}
                held = flagged or bool(base.get('watermark_candidate'))
                row.update(watermark_candidate=bool(held), ready_for_listing=not held,
                           watermark_status='sample_passed' if not held else 'needs_cleanup',
                           pipeline_status='ready_sampled_source' if not held else 'needs_cleanup',
                           sample_only=True, upload_confirmed=bool(self.store), error_code='')
                if held:
                    # Gallery flagging is per property; mask location must still
                    # be established independently on each image before editing.
                    analysis = prop['sample'] if prop.get('sample', {}).get('source_sha256') == digest else self.detector.analyze(data)
                    repaired, reason = self.repair(key, item, data, analysis)
                    if repaired:
                        content = Path(repaired['output_path']).read_bytes()
                        cleaned_url = self.store.put(repaired['key'], content, 'image/webp',
                                                     {'source-sha256': digest, 'status': 'processed'}) if self.store else ''
                        row.update(cleaned_url=cleaned_url, active_url=cleaned_url,
                                   reused_existing=False,
                                   selected_version='cleaned', watermark_status='processed_automated_checks',
                                   pipeline_status='ready_cleaned', ready_for_listing=True,
                                   repair_input_sha256=digest, repair_output_sha256=repaired['output_sha256'],
                                   repair_output_bytes=repaired['output_bytes'], repair_qc=repaired['qc'],
                                   repair_policy_id=repaired['mask_metadata']['policy_id'],
                                   outside_mask_unchanged_before_encoding=True)
                    else:
                        row['error_code'] = reason
                        if reason == 'removal_run_limit':
                            row['pipeline_status'] = 'deferred_cleanup'
                if not self.store:
                    row.update(ready_for_listing=False, pipeline_status='upload_pending', error_code='upload_not_requested')
                prop['images'][number] = row
                self.checkpoint()
            except Exception as exc:
                failures = prop['failures'].get(number, 0) + 1
                prop['failures'][number] = failures
                code = str(exc) if isinstance(exc, PhotoFailure) else 'image_stage_' + type(exc).__name__
                if str(exc) == 'bucket_budget_exceeded':
                    raise PhotoFailure('bucket_budget_exceeded') from None
                if base:
                    prop['images'][number] = {**base, 'reused_existing': True, 'ready_for_listing': False,
                                             'watermark_candidate': bool(flagged or base.get('watermark_candidate')),
                                             'pipeline_status': 'needs_review', 'error_code': code}
                else:
                    prop['images'][number] = {'image_number': int(number), 'original_url': item['original_url'],
                                             'pipeline_status': 'unavailable' if failures >= 3 or code in ('image_http_404', 'image_http_410') else 'retry',
                                             'error_code': code, 'ready_for_listing': False}
                self.counts['image_errors'] += 1
                self.checkpoint()
        images = prop['images']
        pending = any(str(i['image_number']) not in images or images[str(i['image_number'])].get('pipeline_status') in ('retry', 'deferred_cleanup') for i in prop['input_images'])
        held = any(not r.get('ready_for_listing') for r in images.values())
        prop['phase'] = 'pending' if pending or not self.store else 'needs_review' if held else 'complete'
        update = {'property_key': key, 'listing_url': prop['listing_url'], 'watermark_sample': prop.get('sample', {}),
                  'input_signature': prop['input_signature'], 'photo_pipeline_version': VERSION,
                  'photo_pipeline_status': prop['phase'],
                  'images': [r for r in images.values() if r.get('source_copy_url')],
                  'unavailable': [r for r in images.values() if not r.get('source_copy_url')]}
        self.updates[key] = update
        self.checkpoint()
        print(json.dumps({'event': 'property_photos_processed', 'property_key': key, 'phase': prop['phase'],
                          'images': len(images), 'sample': prop.get('sample', {}).get('result', 'all_existing_cleaned')}), flush=True)


def report(worker, output):
    output = Path(output)
    properties = worker.updates
    write_json(output / 'properties.json', {'schema_version': 1, 'properties': properties})
    rows = [{**r, 'property_key': key, 'listing_url': p['listing_url']} for key, p in properties.items() for r in p['images']]
    atomic_write(output / 'images.csv', csv_bytes(rows))
    atomic_write(output / 'watermark_exceptions.csv', csv_bytes([r for r in rows if not r.get('ready_for_listing')]))
    queue = io.StringIO(newline='')
    writer = csv.writer(queue)
    writer.writerow(['property_key', 'phase', 'sample_result', 'sampled_url', 'images', 'ready', 'held'])
    for key, prop in worker.state['properties'].items():
        imgs = list(prop.get('images', {}).values())
        writer.writerow([key, prop['phase'], prop.get('sample', {}).get('result', ''),
                         prop.get('sample', {}).get('sampled_url', ''), len(prop['input_images']),
                         sum(bool(r.get('ready_for_listing')) for r in imgs),
                         sum(not r.get('ready_for_listing') for r in imgs)])
    atomic_write(output / 'property_queue.csv', queue.getvalue().encode())
    for key, prop in properties.items():
        folder = output / 'properties' / Path(*key.split('_', 1))
        write_json(folder / 'property.json', prop)
        atomic_write(folder / 'images.csv', csv_bytes([{**r, 'property_key': key, 'listing_url': prop['listing_url']} for r in prop['images']]))
    summary = {'schema_version': 1, 'pipeline_version': VERSION, **dict(worker.counts),
               'properties_in_report': len(properties), 'ready_images': sum(bool(r.get('ready_for_listing')) for r in rows),
               'held_images': sum(not r.get('ready_for_listing') for r in rows),
               'pending_properties': sum(p['phase'] == 'pending' for p in worker.state['properties'].values()),
               'properties_requiring_review': sum(p['phase'] == 'needs_review' for p in worker.state['properties'].values()),
               'hosted_editing_calls': 0, 'image_proxy_requests': worker.downloader.api_requests,
               'sample_policy': 'One untouched representative image per property; flagged galleries locate a supported mask per image.',
               'delivery': 'uploaded_to_r2' if worker.store else 'prepared_locally_upload_pending'}
    write_json(output / 'summary.json', summary)
    return summary


def enrich_exports(exports, files, state, output, catalog_url=''):
    from daily_scraper import CSV_FIELDS, csv_text
    records = {}
    for path in files or sorted(Path(exports).glob('*/listings.json')):
        for record in json.loads(Path(path).read_text()):
            prop = state['properties'].get(record['property_key'], {})
            images = sorted(prop.get('images', {}).values(), key=lambda r: int(r['image_number']))
            ready = [r['active_url'] for r in images if r.get('ready_for_listing') and r.get('active_url')]
            sample = prop.get('sample', {})
            records[record['property_key']] = {**record, 'source_image_urls': record.get('image_urls', []),
                'image_urls': ready, 'image_count': len(ready), 'photos': images,
                'photo_delivery_status': prop.get('phase', 'pending'), 'ready_image_count': len(ready),
                'held_image_count': sum(not r.get('ready_for_listing') for r in images),
                'photo_catalog_url': catalog_url, 'watermark_status': prop.get('phase', 'pending'),
                'watermark_sample_url': sample.get('sampled_url'),
                'watermark_sample_result': sample.get('result', 'not_checked')}
    rows = list(records.values())
    write_json(Path(output) / 'listings_with_photos.json', rows)
    fields = CSV_FIELDS + ['source_image_urls', 'photo_delivery_status', 'ready_image_count', 'held_image_count', 'photo_catalog_url']
    atomic_write(Path(output) / 'listings_with_photos.csv', csv_text(fields, rows).encode())
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exports', type=Path, default=Path('daily_scraper/exports'))
    parser.add_argument('--listings', type=Path, action='append', default=[])
    parser.add_argument('--state', type=Path, default=Path('daily_scraper/runtime/photo-state.json'))
    parser.add_argument('--output', type=Path, default=Path('daily_scraper/photo_exports'))
    parser.add_argument('--cache', type=Path, default=Path('daily_scraper/runtime/photo-cache'))
    parser.add_argument('--model', type=Path, default=ROOT / 'property_photo_pipeline/models/big-lama.pt')
    parser.add_argument('--legacy-manifest', default='property_images/all_image_urls.csv')
    parser.add_argument('--max-properties', type=int, default=300)
    parser.add_argument('--max-images', type=int, default=1500)
    parser.add_argument('--max-clean-images', type=int, default=300)
    parser.add_argument('--max-image-api-requests', type=int, default=20)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--upload', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_properties <= 10000 or not 1 <= args.max_images <= 100000 or not 0 <= args.max_clean_images <= 10000:
        parser.error('Invalid photo run limits')
    if args.upload and not args.execute:
        parser.error('Upload requires execute')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = prepare(args.state, args.exports, args.listings, args.legacy_manifest)
        settings = r2_settings()
        store = None
        if args.upload:
            if not all(os.environ.get(k) for k in ('R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')):
                write_json(args.output / 'summary.json', {'status': 'blocked_missing_r2_secrets',
                           'pending_properties': sum(p['phase'] == 'pending' for p in state['properties'].values())})
                raise SystemExit('Add R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY as GitHub Actions secrets; photo queue was saved.')
            import boto3
            client = boto3.client('s3', endpoint_url=settings['endpoint_url'], region_name='auto',
                                  aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'])
            store = R2Store(client, settings['bucket_name'], settings['public_base_url'])
        downloader = Downloader(args.cache, urlsplit(settings['public_base_url']).hostname,
                                os.getenv('BRIGHTDATA_API_KEY', ''), os.getenv('BRIGHTDATA_ZONE', 'esstitor_site_phone_scraper'), args.max_image_api_requests)
        worker = PhotoWorker(state, args.state, args.output, store, Detector(), downloader, args.model,
                             args.execute, args.max_images, args.max_clean_images)
        if args.execute and args.max_clean_images:
            cfg = json.loads((ROOT / 'property_photo_pipeline/lama_model.json').read_text())
            if not args.model.exists() or sha(args.model.read_bytes()) != cfg['sha256']:
                raise ValueError('Install the pinned LaMa model before executing cleanup')
        pending = [p for p in state['properties'].values() if p['phase'] == 'pending']
        rank = {'daily': 0, 'backfill': 1, 'legacy': 2}
        pending.sort(key=lambda p: rank.get(p.get('origin'), 3))
        # Persist each property as pending until the shared catalog pointer is
        # confirmed, so an interrupted publication is retried in the next run.
        completed = []
        for prop in pending[:args.max_properties]:
            worker.process(prop)
            if prop['phase'] != 'pending':
                completed.append((prop, prop['phase']))
                prop['phase'] = 'pending'
                worker.checkpoint()
            if worker.counts['images_processed'] >= args.max_images:
                break
        summary = report(worker, args.output)
        if store and worker.updates:
            publication = store.publish(worker.updates)
            write_json(args.output / 'delivery.json', publication)
            for prop, phase in completed:
                prop['phase'] = phase
            worker.checkpoint()
            summary = report(worker, args.output)
        elif store:
            for prop, phase in completed:
                prop['phase'] = phase
            worker.checkpoint()
            summary = report(worker, args.output)
        enrich_exports(args.exports, args.listings, state, args.output,
                       store.public + '/graden-imports/property-photos/latest.json' if store else '')
        if store:
            delivery = store.publish_daily(args.output)
            write_json(args.output / 'daily_delivery.json', delivery)
        with zipfile.ZipFile(args.output / 'property-photos.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in args.output.rglob('*'):
                relative = path.relative_to(args.output)
                if path.is_file() and relative.parts[0] not in ('ready', 'repairs') and path.suffix in ('.json', '.csv'):
                    archive.write(path, str(relative))
        print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    main()
