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
from photo_queue import prepare, enqueue, ordered_pending
from brightdata_browser import BrowserAPI, BrowserFailure
from url_utils import transport_url
from watermark_policy import POLICY,analysis_record,checked,decorate,listing_summary,agency_hits,INCIDENT_PROPERTIES

VERSION = POLICY
OGLASI_VERSION = POLICY+'-oglasi'
HOSTS = {'content.estitor.com', 'patuljak.me', 'www.patuljak.me', 'www.realitica.com', 'realitica.com', 'slike.oglasi.me'}
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
    def __init__(self, cache, public_host, key='', zone='esstitor_site_phone_scraper', max_api=100):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.hosts = HOSTS | {public_host}
        self.opener = build_opener(SafeRedirect(self.hosts))
        self.next_request, self.paused = {}, set()
        self.key, self.zone, self.max_api, self.api_requests = key, zone, max_api, 0
        self.browser = BrowserAPI(key, os.getenv('BRIGHTDATA_BROWSER_ZONE', 'estitorscraper2'), self.hosts, 20_000_000) if key else None
        self.browser_hosts = set()

    def browser_image(self, url):
        if not self.browser or self.api_requests >= self.max_api:
            raise PhotoFailure('image_api_request_limit')
        for attempt in range(2):
            if self.api_requests >= self.max_api:
                raise PhotoFailure('image_api_request_limit')
            self.api_requests += 1
            try:
                data, _, _ = self.browser.get_image(url)
                decode(data)
                self.browser_hosts.add(urlsplit(url).hostname)
                return data
            except BrowserFailure as exc:
                if str(exc) == 'browser_source_http_429':
                    self.paused.add(urlsplit(url).hostname)
                if attempt == 0 and str(exc) in ('browser_navigate_failed', 'browser_navigate_timeout', 'browser_content_failed',
                                               'browser_content_timeout', 'browser_session_closed', 'browser_navigation_aborted',
                                               'browser_connect_timeout', 'browser_connect_failed'):
                    time.sleep(1)
                    continue
                raise PhotoFailure('image_' + str(exc)) from None

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
        if host in self.browser_hosts:
            data = self.browser_image(url)
            atomic_write(path, data)
            return data
        request = Request(transport_url(url), headers={'User-Agent': 'Mozilla/5.0 (compatible; GRADEN-PropertyPhotos/1.0)'})
        try:
            with self.opener.open(request, timeout=25) as response:
                data = response.read(20_000_001)
        except HTTPError as exc:
            if exc.code == 429:
                self.paused.add(host)
                raise PhotoFailure('image_http_429') from None
            if exc.code == 403 and self.key and self.api_requests >= self.max_api:
                raise PhotoFailure('image_api_request_limit') from None
            if exc.code != 403 or not self.key or host not in HOSTS:
                raise PhotoFailure('image_http_' + str(exc.code)) from None
            # Raw API mode is required for original image bytes, not JSON HTML.
            payload = json.dumps({'zone': self.zone, 'url': transport_url(url), 'format': 'raw', 'method': 'GET'}).encode()
            self.api_requests += 1
            api = Request('https://api.brightdata.com/request', data=payload,
                          headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            try:
                with build_opener(SafeRedirect({'api.brightdata.com'})).open(api, timeout=45) as response:
                    data = response.read(20_000_001)
                try:
                    decode(data)
                except Exception:
                    # A raw proxy response can still be a CloudFront/Cloudflare
                    # HTML block with HTTP 200. Fetch original binary response
                    # bytes through the managed browser, never a screenshot.
                    data = self.browser_image(url)
            except PhotoFailure:
                raise
            except Exception:
                raise PhotoFailure('image_proxy_fetch_failed') from None
        except PhotoFailure:
            raise
        except Exception:
            raise PhotoFailure('image_connection_failed') from None
        if len(data) > 20_000_000:
            raise PhotoFailure('image_bytes_limit')
        try:
            decode(data)
        except Exception:
            if self.browser and host in HOSTS:
                data = self.browser_image(url)
            else:
                raise PhotoFailure('image_response_not_image') from None
        atomic_write(path, data)
        return data


def decode(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise PhotoFailure('image_pixel_limit')
        return np.asarray(ImageOps.exif_transpose(image).convert('RGB'))


def uncertain_text_sample(sample):
    """Only generic text uncertainty qualifies; known logo/brand evidence stays held."""
    try:
        return (sample.get('result') == 'needs_review' and sample.get('reason') == 'unclassified_text'
                and not sample.get('hits') and float(sample.get('score', 1)) < .65)
    except (TypeError, ValueError):
        return False


def requeue_uncertain_text(state):
    for prop in state['properties'].values():
        if (prop.get('phase') != 'needs_review' or not uncertain_text_sample(prop.get('sample', {}))
                or prop.get('individual_text_policy') == VERSION):
            continue
        for row in prop.get('images', {}).values():
            if not row.get('ready_for_listing') and row.get('source_copy_url') and row.get('error_code') == 'unsupported_or_uncertain_mask':
                row.update(upload_confirmed=False, pipeline_status='retry')
        prop.update(phase='pending', individual_text_policy=VERSION)


def requeue_oglasi(state):
    for key,prop in state['properties'].items():
        if not key.startswith('oglasi_') or prop.get('photo_pipeline_version') == OGLASI_VERSION:
            continue
        for row in prop.get('images',{}).values():
            if not is_cleaned(row):
                row.update(upload_confirmed=False,ready_for_listing=False,pipeline_status='retry')
        prop.update(phase='pending',sample={},photo_pipeline_version=OGLASI_VERSION)


def requeue_per_image(state):
    for key,prop in state['properties'].items():
        if prop.get('per_image_policy')==POLICY:
            continue
        prop['per_image_policy']=POLICY
        prop['sample']={}
        rows=prop.get('images',{})
        if any(not checked(rows.get(str(i['image_number']),{})) for i in prop.get('input_images',[])):
            prop['phase']='pending'
            for row in rows.values():
                if not checked(row):
                    row['upload_confirmed']=False
                    if not is_cleaned(row):row.update(ready_for_listing=False,pipeline_status='retry')
        if key in INCIDENT_PROPERTIES:
            for row in rows.values():row.update(publication_hold=True,ready_for_listing=False)


class Detector:
    def __init__(self, template=None):
        folder = ROOT / 'property_photo_pipeline/reference_assets'
        config = json.loads((folder / 'patuljak_mascot.json').read_text())
        data = (folder / config['template_file']).read_bytes()
        if sha(data) != config['template_sha256']:
            raise ValueError('Template digest mismatch')
        self.template = np.asarray(Image.open(io.BytesIO(data)).convert('RGB')) if template is None else template

    def analyze(self, data, mask_only=False, source=None):
        rgb = decode(data)
        height, width = rgb.shape[:2]
        if min(width, height) < 100:
            return {'result': 'needs_review', 'reason': 'small_preview_only', 'score': 0, 'box': [], 'hits': []}
        if source == 'oglasi':
            from portal_watermarks import build_mask as portal_mask
            _, meta = portal_mask(rgb, 'oglasi')
            words = ocr(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), 'tesseract', 30)
            hits = brand_hits(words, width, height)+agency_hits(words,width,height)
            text_found = any(w['confidence'] >= 45 and len(token(w['text'])) >= 3 for w in words)
            outside = False
            if meta['eligible']:
                x1,y1,x2,y2 = meta['overlay_box_original_pixels']
                for word in words:
                    a,b,c,d=word['box_original_pixels']
                    if word['confidence'] >= 60 and len(token(word['text'])) >= 4 and not (
                            x1-8 <= (a+c)/2 <= x2+8 and y1-8 <= (b+d)/2 <= y2+8):
                        outside = True
            known = meta['eligible'] or bool(hits)
            # Oglasi's CDN originals carry faint stamps that OCR can miss. A
            # missing calibrated pose must stay held, even when OCR sees no text.
            return {'result':'watermark_candidate' if known else 'needs_review',
                    'reason':'oglasi_calibrated_center' if meta['eligible'] else 'unclassified_text' if text_found else 'oglasi_expected_stamp_not_located',
                    'score':0., 'box':[], 'hits':hits, 'width':width, 'height':height,
                    'other_text_detected':text_found, 'unsupported_text_outside_portal':outside,
                    'portal_mask_metadata':meta}
        try:
            score, box, _ = match_image(rgb, self.template)
        except ValueError:
            score, box = 0., []
        # Gallery flagging already came from the property's full sample. Strong
        # geometry needs no corroborating OCR; geometry below .65 cannot qualify
        # under the existing mask policy even if OCR finds text.
        if mask_only and (score < .65 or score >= .85):
            return {'result': 'watermark_candidate' if score >= .85 else 'needs_review',
                    'reason': 'known_logo_geometry' if score >= .85 else 'no_supported_mask_geometry',
                    'score': float(score), 'box': box, 'hits': [], 'width': width, 'height': height}
        words = ocr(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), 'tesseract', 30)
        hits = brand_hits(words, width, height)+agency_hits(words,width,height)
        text_found = any(w['confidence'] >= 45 and len(token(w['text'])) >= 3 for w in words)
        known = score >= .85 or bool(hits)
        return {'result': 'watermark_candidate' if known else 'needs_review' if text_found else 'sample_passed',
                'reason': 'known_logo_or_brand' if known else 'unclassified_text' if text_found else 'no_detected_mark_in_sample',
                'score': float(score), 'box': box, 'hits': hits,
                'width': width, 'height': height, 'other_text_detected': text_found}

    def for_removal(self, data, source=None):
        return self.analyze(data, mask_only=True, source=source)

    def for_property(self,data,source,delivered=False):
        result=self.analyze(data,source='oglasi' if source=='oglasi' else None)
        if source=='patuljak' and not delivered and result.get('result')=='sample_passed':
            result={**result,'result':'needs_review','reason':'patuljak_expected_stamp_not_located'}
        return result


class PhotoWorker:
    def __init__(self, state, state_path, output, store, detector, downloader, model_path,
                 execute=False, max_images=1500, max_clean=300, max_runtime=2700):
        self.state, self.state_path, self.output = state, Path(state_path), Path(output)
        self.store, self.detector, self.downloader = store, detector, downloader
        self.model_path, self.execute = Path(model_path), execute
        self.max_images, self.max_clean = max_images, max_clean
        self.infer, self.model_hash = None, None
        self.counts, self.updates = Counter(), {}
        self.deadline = time.monotonic() + max_runtime
        self.catalog, _ = store.snapshot() if store else ({'properties': {}}, None)

    def time_exhausted(self):
        if time.monotonic() >= self.deadline:
            self.counts['runtime_limit_reached'] = 1
            return True
        return False

    def checkpoint(self):
        write_json(self.state_path, self.state)

    def repair(self, key, item, data, analysis):
        if any(h.get('kind')=='agency' for h in analysis.get('hits',[])):
            return None,'agency_watermark_requires_supported_mask'
        if key.startswith('oglasi_'):
            return self.repair_portal(key, item, data, analysis)
        if self.time_exhausted():
            return None, 'runtime_run_limit'
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

    def repair_portal(self, key, item, data, analysis):
        from portal_watermarks import build_mask as portal_mask, repair as portal_repair, VERSION as portal_version
        if analysis.get('unsupported_text_outside_portal') or any(
                hit.get('brand') not in (None,'oglasi') for hit in analysis.get('hits',[])):
            return None, 'multiple_or_unsupported_watermarks'
        rgb=decode(data);mask,meta=portal_mask(rgb,'oglasi')
        if not meta['eligible']:
            return None,'unsupported_or_uncertain_mask'
        if not self.execute:
            return None,'removal_execute_required'
        if self.time_exhausted():
            return None,'runtime_run_limit'
        if self.counts['removal_attempts'] >= self.max_clean:
            return None,'removal_run_limit'
        if self.infer is None:
            cfg=json.loads((ROOT/'property_photo_pipeline/lama_model.json').read_text())
            self.model_hash=sha(self.model_path.read_bytes())
            if self.model_hash != cfg['sha256']:
                raise PhotoFailure('lama_model_digest_mismatch')
            self.infer=TorchInpainter(self.model_path,2)
        self.counts['removal_attempts']+=1
        encoded,proof=portal_repair(rgb,mask,meta,self.infer,self.model_hash,sha(data))
        if encoded is None:
            return None,'repair_automated_checks_failed'
        source,ident=key.split('_',1);digest=sha(encoded)
        object_key=f'properties/{source}/{ident}/cleaned/{item["image_number"]:03d}-{digest}.webp'
        path=self.output/'ready'/object_key;atomic_write(path,encoded)
        self.counts['cleaned']+=1
        return {'output_path':str(path),'output_sha256':digest,'output_bytes':len(encoded),'key':object_key,
                'qc':proof,'mask_metadata':{'policy_id':portal_version}},''

    def analyze_property(self, data, key, removal=False,delivered=False):
        if hasattr(self.detector,'for_property'):
            return self.detector.for_property(data,key.split('_',1)[0],delivered)
        if key.startswith('oglasi_'):
            return self.detector.analyze(data, source='oglasi')
        return self.detector.analyze(data)

    def process(self, prop):
        key = prop['property_key']
        version=OGLASI_VERSION if key.startswith('oglasi_') else VERSION
        current = self.catalog.get('properties', {}).get(key, {})
        existing = {str(r['image_number']): r for r in current.get('images', [])}
        if current.get('photo_pipeline_version') == version and current.get('input_signature') == prop['input_signature']:
            prop['sample'] = current.get('watermark_sample', {})
            for number, row in existing.items():
                if row.get('pipeline_status') not in ('retry', 'deferred_cleanup'):
                    prop['images'][number] = {**copy.deepcopy(row), 'reused_existing': True, 'upload_confirmed': True}
        for number, row in existing.items():
            if is_cleaned(row):
                prop['images'][number] = {**copy.deepcopy(row), 'reused_existing': True,
                                          'ready_for_listing':not row.get('publication_hold'), 'pipeline_status': 'ready_existing_cleaned'}
        unprocessed = [i for i in prop['input_images'] if not checked(prop['images'].get(str(i['image_number']), {}))]
        if not prop['input_images']:
            prop['phase'] = 'no_images'
            self.checkpoint()
            return
        known_pending = any(r.get('watermark_candidate') and not is_cleaned(r) for r in existing.values())
        if prop.get('sample', {}).get('retry_required') or (prop.get('sample') and not prop['sample'].get('source_sha256')):
            prop['sample'] = {}
        if unprocessed and not prop.get('sample'):
            for attempted, first in enumerate(unprocessed, 1):
                if self.time_exhausted():
                    prop['phase'] = 'pending'
                    self.checkpoint()
                    return
                base = existing.get(str(first['image_number']), {})
                sample_url = base.get('source_copy_url') or first['original_url']
                try:
                    try:
                        data = self.downloader.get(sample_url)
                    except Exception:
                        if not base.get('source_copy_url'):
                            raise
                        sample_url = first['original_url']
                        data = self.downloader.get(sample_url)
                    if base:
                        expected = base['optimized_sha256'] if sample_url == base.get('source_copy_url') else base['source_sha256']
                        if sha(data) != expected:
                            raise PhotoFailure('sample_source_identity_mismatch')
                    analysis = self.analyze_property(data, key)
                    prop['sample'] = {**analysis, 'image_number': first['image_number'], 'sampled_url': sample_url,
                                      'policy':POLICY,
                                      'source_sha256': sha(data), 'images_sampled': 1, 'images_attempted': attempted,
                                      'meaning': 'One source image checked; remaining images are not individually certified.'}
                    self.counts['properties_sampled'] += 1
                    break
                except Exception as exc:
                    code = str(exc) if isinstance(exc, PhotoFailure) else 'sample_analysis_failed'
                    prop['sample'] = {'result': 'needs_review', 'reason': code,
                                      'image_number': first['image_number'], 'sampled_url': sample_url,
                                      'images_sampled': 0, 'images_attempted': attempted, 'retry_required': True}
                    if code in ('image_api_request_limit', 'image_http_429', 'image_host_rate_limited'):
                        break
        flagged = known_pending or prop.get('sample', {}).get('result') != 'sample_passed'
        individual_unknown = uncertain_text_sample(prop.get('sample', {}))
        for item in prop['input_images']:
            if self.time_exhausted():
                break
            number = str(item['image_number'])
            done = prop['images'].get(number)
            if done and done.get('upload_confirmed') and checked(done) and done.get('pipeline_status') != 'deferred_cleanup':
                continue
            base = copy.deepcopy(done or existing.get(number, {}))
            if self.counts['images_processed'] >= self.max_images:
                break
            self.counts['images_processed'] += 1
            try:
                data = self.downloader.get(item['original_url'])
                digest = sha(data)
                if base and digest != base.get('source_sha256'):
                    prop['images'][number] = {**base, 'original_url': item['original_url'], 'reused_existing': True,
                                             'ready_for_listing': False, 'publication_hold':True, 'pipeline_status': 'needs_review',
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
                cached=base.get('watermark_analysis') or {}
                analysis=(cached.get('analysis') if checked(base) and cached.get('source_sha256')==digest else None)
                if not analysis:
                    analysis=(prop['sample'] if prop.get('sample',{}).get('policy')==POLICY and prop.get('sample',{}).get('source_sha256')==digest
                              else self.analyze_property(data,key))
                row['watermark_analysis']=analysis_record(analysis,digest)
                if base.get('watermark_candidate') and not is_cleaned(base) and row['watermark_analysis']['status']=='clean':
                    row['watermark_analysis']['status']='unknown'
                    row['watermark_analysis']['reason']='previous_watermark_flag_unresolved'
                row['sample_only']=False
                row['verification_scope']='per_image_original_and_delivered_checks'
                held=row['watermark_analysis']['status']!='clean'
                row.update(watermark_candidate=held,ready_for_listing=not held,
                           watermark_status=row['watermark_analysis']['status'],
                           pipeline_status='needs_cleanup' if held else 'ready_individually_checked_source',
                           upload_confirmed=bool(self.store),error_code='')
                if is_cleaned(base):
                    # Existing edits are inspected as delivered, not assumed clear.
                    row.update(selected_version='cleaned',active_url=base['cleaned_url'],reused_existing=True)
                    delivered=self.downloader.get(base['cleaned_url'])
                    if sha(delivered)!=base.get('repair_output_sha256',base['cleaned_url'].rsplit('-',1)[-1].split('.')[0]):
                        raise PhotoFailure('cleaned_output_identity_mismatch')
                    out=self.analyze_property(delivered,key,delivered=True)
                    if key.startswith('oglasi_') and out.get('reason')=='oglasi_expected_stamp_not_located':
                        out={**out,'result':'sample_passed','reason':'delivered_portal_stamp_not_detected'}
                    row['delivered_watermark_analysis']=analysis_record(out,sha(delivered))
                    hold=(row['delivered_watermark_analysis']['status']!='clean' or key in INCIDENT_PROPERTIES)
                    row.update(publication_hold=hold,ready_for_listing=not hold,
                               pipeline_status='needs_review' if hold else 'ready_existing_cleaned',
                               error_code='reported_image_requires_residual_review' if key in INCIDENT_PROPERTIES else
                                          'residual_mark_or_unclassified_overlay' if hold else '')
                elif held:
                    repaired,reason=self.repair(key,item,data,analysis)
                    if repaired:
                        content=Path(repaired['output_path']).read_bytes()
                        out=self.analyze_property(content,key,delivered=True)
                        if key.startswith('oglasi_') and out.get('reason')=='oglasi_expected_stamp_not_located':
                            out={**out,'result':'sample_passed','reason':'delivered_portal_stamp_not_detected'}
                        output_analysis=analysis_record(out,sha(content))
                        clear=output_analysis['status']=='clean'
                        cleaned_url=self.store.put(repaired['key'],content,'image/webp',
                                                  {'source-sha256':digest,'status':'processed'}) if self.store else ''
                        row.update(cleaned_url=cleaned_url,active_url=cleaned_url,selected_version='cleaned',
                                   reused_existing=False,watermark_status='cleaned' if clear else 'watermarked',
                                   pipeline_status='ready_cleaned' if clear else 'needs_review',ready_for_listing=clear,
                                   publication_hold=not clear,repair_input_sha256=digest,
                                   repair_output_sha256=repaired['output_sha256'],repair_output_bytes=repaired['output_bytes'],
                                   repair_qc=repaired['qc'],repair_policy_id=repaired['mask_metadata']['policy_id'],
                                   outside_mask_unchanged_before_encoding=True,delivered_watermark_analysis=output_analysis,
                                   error_code='' if clear else 'residual_mark_or_unclassified_overlay')
                    else:
                        row['error_code']=reason
                        if reason in ('removal_run_limit','runtime_run_limit'):
                            row['pipeline_status']='deferred_cleanup'
                decorate(row)
                if not self.store:
                    row.update(ready_for_listing=False, pipeline_status='upload_pending', error_code='upload_not_requested')
                prop['images'][number] = row
                self.checkpoint()
            except Exception as exc:
                code = str(exc) if isinstance(exc, PhotoFailure) else 'image_stage_' + type(exc).__name__
                if str(exc) == 'bucket_budget_exceeded':
                    raise PhotoFailure('bucket_budget_exceeded') from None
                failures = prop['failures'].get(number, 0) + (code != 'image_api_request_limit')
                prop['failures'][number] = failures
                if base:
                    prop['images'][number] = {**base, 'reused_existing': True, 'ready_for_listing': False,
                                             'publication_hold':True,
                                             'watermark_candidate': bool(flagged or base.get('watermark_candidate')),
                                             'pipeline_status': 'needs_review', 'error_code': code}
                else:
                    prop['images'][number] = {'image_number': int(number), 'original_url': item['original_url'],
                                             'pipeline_status': 'unavailable' if failures >= 3 or code in ('image_http_404', 'image_http_410') else 'retry',
                                             'error_code': code, 'ready_for_listing': False}
                self.counts['deferred_by_limits' if code == 'image_api_request_limit' else 'image_errors'] += 1
                self.checkpoint()
        images = prop['images']
        pending = any(str(i['image_number']) not in images or images[str(i['image_number'])].get('pipeline_status') in ('retry', 'deferred_cleanup') for i in prop['input_images'])
        held = any(not r.get('ready_for_listing') for r in images.values())
        prop['phase'] = 'pending' if pending or not self.store else 'needs_review' if held else 'complete'
        update = {'property_key': key, 'listing_url': prop['listing_url'], 'watermark_sample': prop.get('sample', {}),
                  'input_signature': prop['input_signature'], 'photo_pipeline_version': version,
                  'photo_pipeline_status': prop['phase'],
                  'images': [r for r in images.values() if r.get('source_copy_url')],
                  'unavailable': [r for r in images.values() if not r.get('source_copy_url')]}
        update.update(listing_summary(list(images.values()),len(prop['input_images']),key))
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
    atomic_write(output / 'watermark_audit.csv', csv_bytes(rows))
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
               'sample_policy': 'Every original and every delivered edit is checked independently; representative samples are compatibility metadata.',
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
            from watermark_policy import attach_photos
            records[record['property_key']]=attach_photos(record,{
                **prop,'images':images,'watermark_sample':sample,'photo_pipeline_status':prop.get('phase','pending')})
            records[record['property_key']]['photo_catalog_url']=catalog_url
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
    parser.add_argument('--max-image-api-requests', type=int, default=100)
    parser.add_argument('--max-runtime-seconds', type=int, default=2700)
    parser.add_argument('--priority-properties', default='', help='Comma-separated property keys to recover first')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--upload', action='store_true')
    args = parser.parse_args()
    priority = [key for key in re.split(r'[,\s]+', args.priority_properties.strip()) if key]
    if any(not re.fullmatch(r'(estitor|patuljak|realitica|oglasi)_\d+', key) for key in priority):
        parser.error('Invalid priority property key')
    if not 1 <= args.max_properties <= 10000 or not 1 <= args.max_images <= 100000 or not 0 <= args.max_clean_images <= 10000 or args.max_runtime_seconds < 1:
        parser.error('Invalid photo run limits')
    if args.upload and not args.execute:
        parser.error('Upload requires execute')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = prepare(args.state, args.exports, args.listings, args.legacy_manifest)
        requeue_uncertain_text(state)
        requeue_oglasi(state)
        requeue_per_image(state)
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
                             args.execute, args.max_images, args.max_clean_images, args.max_runtime_seconds)
        if args.execute and args.max_clean_images:
            cfg = json.loads((ROOT / 'property_photo_pipeline/lama_model.json').read_text())
            if not args.model.exists() or sha(args.model.read_bytes()) != cfg['sha256']:
                raise ValueError('Install the pinned LaMa model before executing cleanup')
        pending = ordered_pending(state, priority)
        # Persist each property as pending until the shared catalog pointer is
        # confirmed, so an interrupted publication is retried in the next run.
        completed = []
        for prop in pending[:args.max_properties]:
            if worker.time_exhausted():
                break
            worker.process(prop)
            if prop['phase'] != 'pending':
                completed.append((prop, prop['phase']))
                prop['phase'] = 'pending'
                worker.checkpoint()
            if worker.counts['images_processed'] >= args.max_images or worker.time_exhausted():
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
        enriched=enrich_exports(args.exports, args.listings, state, args.output,
                       store.public + '/graden-imports/property-photos/latest.json' if store else '')
        if store and enriched:
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
