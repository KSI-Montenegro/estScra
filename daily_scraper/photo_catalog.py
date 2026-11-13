"""Append photo deliveries to the shared R2 catalog without losing cleaned selections."""
import copy
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit

OWNER = 'estScra-property-photo-import'
PREFIX = 'graden-imports/property-photos'
FIELDS = ['property_key', 'listing_url', 'image_number', 'original_url', 'source_copy_url',
          'active_url', 'cleaned_url', 'selected_version', 'watermark_status', 'watermark_candidate',
          'ready_for_listing', 'pipeline_status', 'source_sha256', 'optimized_sha256', 'bytes',
          'repair_output_sha256', 'error_code', 'image_url', 'uploaded_url', 'optimized_bytes',
          'upload_status', 'repair_status', 'watermark_check_status', 'watermark_portals',
          'watermark_agencies', 'watermark_brands', 'publication_hold', 'verification_scope']


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def csv_bytes(records):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, FIELDS, extrasaction='ignore')
    writer.writeheader()
    for record in records:
        compatible = {**record, 'image_url': record.get('original_url', ''),
                      'uploaded_url': record.get('source_copy_url', ''), 'optimized_bytes': record.get('bytes', ''),
                      'upload_status': 'existing' if record.get('source_copy_url') else '',
                      'repair_status': 'processed' if is_cleaned(record) else 'pending'}
        row = {k: compatible.get(k, '') for k in FIELDS}
        for key, value in row.items():
            if isinstance(value,(list,dict)):
                value=json.dumps(value,ensure_ascii=False);row[key]=value
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                row[key] = "'" + value
        writer.writerow(row)
    return ('\ufeff' + out.getvalue()).encode()


def is_cleaned(image):
    return image.get('selected_version') == 'cleaned' and bool(image.get('cleaned_url'))


def reviewed_repair_matches(row):
    """Accept an explicitly reviewed re-edit, bound to both source and output bytes."""
    from watermark_policy import POLICY, checked
    proof = row.get('repair_qc')
    if row.get('repair_policy_id') != 'patuljak-footer-reviewed-v3-expanded' or not isinstance(proof, dict):
        return False
    root = Path(__file__).resolve().parent
    root = root.parent if (root.parent / 'property_photo_pipeline').exists() else root
    model = json.loads((root / 'property_photo_pipeline/lama_model.json').read_text())
    config = json.loads((root / 'property_photo_pipeline/reference_assets/patuljak_overlay_mask.json').read_text())
    out = row.get('delivered_watermark_analysis') or {}
    review = proof.get('visual_review') or {}
    return bool('/properties/patuljak/' in row.get('cleaned_url', '')
                and checked(row)
                and row.get('repair_input_sha256') == row.get('source_sha256') == proof.get('source_sha256')
                and proof.get('output_sha256') == row.get('repair_output_sha256')
                and proof.get('output_bytes') == row.get('repair_output_bytes')
                and 0 < row.get('repair_output_bytes', 0) <= 400000
                and proof.get('model_sha256') == model['sha256']
                and proof.get('canonical_mask_sha256') == config['mask_asset_sha256']
                and re.fullmatch(r'[a-f0-9]{64}', proof.get('mask_sha256', ''))
                and proof.get('outside_mask_unchanged_before_encoding') is True
                and row.get('outside_mask_unchanged_before_encoding') is True
                and proof.get('dimensions_preserved_before_encoding') is True
                and isinstance(proof.get('residual_logo_score'), (int, float))
                and 0 <= proof['residual_logo_score'] < .65
                and (proof.get('focused_ocr') or {}).get('brand_detected') is False
                and out.get('policy') == POLICY and out.get('status') == 'clean'
                and out.get('source_sha256') == row.get('repair_output_sha256')
                and review.get('method') == 'assistant_visual_full_image'
                and review.get('decision') == 'no_visible_watermark'
                and review.get('output_sha256') == row.get('repair_output_sha256'))


def repair_proof_matches(row):
    if (row.get('repair_input_sha256') != row.get('source_sha256') or
            row.get('outside_mask_unchanged_before_encoding') is not True):
        return False
    if row.get('repair_policy_id') == 'patuljak-footer-stable-colors-v2-filled-word-bands':
        return row.get('repair_qc') == 'mascot_and_ocr_only_pass_text_shape_unvalidated'
    if reviewed_repair_matches(row):
        return True
    if row.get('repair_policy_id') != 'portal-centers-v2-byte-optimized':
        return False
    proof=row.get('repair_qc')
    if not isinstance(proof,dict) or '/properties/oglasi/' not in row.get('cleaned_url',''):
        return False
    root=Path(__file__).resolve().parent
    root=root.parent if (root.parent/'property_photo_pipeline').exists() else root
    model=json.loads((root/'property_photo_pipeline/lama_model.json').read_text())
    reference=(root/'property_photo_pipeline/reference_assets/portal_centers.json').read_bytes()
    meta=proof.get('mask_metadata') or {}
    if (proof.get('policy') != row['repair_policy_id'] or proof.get('qc') != 'portal_word_shapes_and_focused_ocr_passed'
            or proof.get('source_sha256') != row['source_sha256'] or proof.get('model_sha256') != model['sha256']
            or proof.get('outside_mask_unchanged_before_encoding') is not True
            or proof.get('dimensions_preserved_before_encoding') is not True
            or proof.get('output_bytes') != row.get('repair_output_bytes')
            or not 0 < row.get('repair_output_bytes',0) <= 400000
            or (proof.get('focused_ocr') or {}).get('brand_detected') is not False
            or meta.get('eligible') is not True or meta.get('reference_config_sha256') != sha(reference)
            or not re.fullmatch(r'[a-f0-9]{64}',proof.get('mask_sha256',''))
            or proof.get('mask_sha256') != meta.get('mask_sha256')):
        return False
    stamps=meta.get('stamps')
    if not isinstance(stamps,list) or len(stamps)!=1 or stamps[0].get('brand')!='oglasi' or stamps[0].get('eligible') is not True:
        return False
    for key in ('residual_pre_encoding','residual_post_encoding'):
        checks=proof.get(key)
        if not isinstance(checks,list) or len(checks)!=1:
            return False
        check=checks[0]
        if (check.get('brand')!='oglasi' or check.get('passed') is not True
                or not isinstance(check.get('before'),(int,float)) or not isinstance(check.get('after'),(int,float))
                or not -1 <= check['after'] < min(.10,check['before']*.5)):
            return False
    return True


def merge_catalog(current, updates):
    result = copy.deepcopy(current)
    props = result.setdefault('properties', {})
    for key, delta in updates.items():
        old = props.get(key, {})
        images = {int(r['image_number']): copy.deepcopy(r) for r in old.get('images', [])}
        if len(images) != len(old.get('images', [])):
            raise ValueError('Duplicate images in current catalog')
        for row in delta.get('images', []):
            number = int(row['image_number'])
            prior = images.get(number)
            if prior and is_cleaned(prior):
                if row.get('supersedes_output_sha256') and row.get('repair_output_sha256') != prior.get('repair_output_sha256'):
                    if (row['supersedes_output_sha256'] != prior.get('repair_output_sha256')
                            or row.get('source_sha256') != prior.get('source_sha256')
                            or not reviewed_repair_matches(row)):
                        raise ValueError('Reviewed replacement does not match the current source and cleaned version')
                    replacement = copy.deepcopy(row)
                    replacement['previous_cleaned_versions'] = copy.deepcopy(prior.get('previous_cleaned_versions', [])) + [{
                        'cleaned_url': prior['cleaned_url'], 'output_sha256': prior['repair_output_sha256'],
                        'publication_hold': True, 'reason': 'superseded_by_reviewed_reclean'}]
                    images[number] = replacement
                    continue
                if (reviewed_repair_matches(prior) and row.get('source_sha256') == prior.get('source_sha256')
                        and row.get('repair_output_sha256') != prior.get('repair_output_sha256')):
                    # A worker restored before the re-edit must not reselect or
                    # attach old output checks to the newly reviewed bytes.
                    continue
                from watermark_policy import checked
                if checked(row) and row.get('source_sha256')==prior.get('source_sha256'):
                    # Refresh evidence and readiness while preserving immutable edits.
                    immutable={k:prior[k] for k in ('cleaned_url','active_url','selected_version',
                               'repair_output_sha256','repair_output_bytes') if k in prior}
                    prior.update(copy.deepcopy(row));prior.update(immutable)
                # Keep stored edits; new residual evidence can revoke readiness.
                if row.get('publication_hold'):
                    prior.update(publication_hold=True,ready_for_listing=False,pipeline_status='needs_review',
                                 error_code=row.get('error_code','publication_hold'),
                                 watermark_analysis=copy.deepcopy(row.get('watermark_analysis',prior.get('watermark_analysis',{}))))
                else:
                    prior['ready_for_listing'] = not prior.get('publication_hold',False)
                    prior['pipeline_status'] = 'needs_review' if prior.get('publication_hold') else 'ready_existing_cleaned'
                continue
            if prior and row.get('source_sha256') != prior.get('source_sha256'):
                prior.update(ready_for_listing=False, pipeline_status='needs_review',
                             error_code='source_identity_changed')
                continue
            images[number] = copy.deepcopy(row)
        unavailable = {int(r['image_number']): r for r in old.get('unavailable', [])}
        for row in delta.get('unavailable', []):
            if int(row['image_number']) not in images:
                unavailable[int(row['image_number'])] = copy.deepcopy(row)
        for number in images:
            unavailable.pop(number, None)
        props[key] = {**old, **{k: copy.deepcopy(v) for k, v in delta.items() if k not in ('images', 'unavailable')},
                      'images': [images[n] for n in sorted(images)],
                      'unavailable': [unavailable[n] for n in sorted(unavailable)]}
    from watermark_policy import listing_summary
    for key,prop in props.items():
        expected=max(prop.get('photo_expected_count',0),len(prop.get('images',[]))+len(prop.get('unavailable',[])))
        prop.update(listing_summary(prop.get('images',[]),expected,key))
    rows = [r for p in props.values() for r in p.get('images', [])]
    from watermark_policy import decorate
    for row in rows:
        if is_cleaned(row):
            row['ready_for_listing'] = not row.get('publication_hold',False)
            row.setdefault('pipeline_status', 'ready_existing_cleaned')
            match = re.search(r'-([0-9a-f]{64})\.webp$', row['cleaned_url'])
            if match:
                row.setdefault('repair_output_sha256', match.group(1))
        else:
            row.setdefault('ready_for_listing', False)
            row.setdefault('pipeline_status', 'needs_cleanup' if row.get('watermark_candidate') else 'pending_property_sample')
        decorate(row)
    result.update(schema_version=1, updated_at=datetime.now(timezone.utc).isoformat(),
                  daily_photo_pipeline_version=1, available_images=len(rows),
                  unavailable_images=sum(len(p.get('unavailable', [])) for p in props.values()),
                  cleaned_images=sum(is_cleaned(r) for r in rows),
                  watermark_candidates=sum(bool(r.get('watermark_candidate')) for r in rows),
                  remaining_candidates=sum(bool(r.get('watermark_candidate') or r.get('publication_hold')) and not r.get('ready_for_listing') for r in rows),
                  ready_images=sum(r.get('ready_for_listing') is True for r in rows),
                  note='Ready source images require per-image checks. Cleaned files with residual holds are excluded from ready galleries.')
    return result


class R2Store:
    def __init__(self, client, bucket, public_base, budget=8_000_000_000):
        self.client, self.bucket = client, bucket
        self.public = public_base.rstrip('/')
        if urlsplit(self.public).scheme != 'https' or urlsplit(self.public).username:
            raise ValueError('HTTPS public delivery base required')
        self.budget, self.used, self.keys = budget, None, {}

    def head(self, key):
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') in ('404', 'NotFound', 'NoSuchKey'):
                return None
            raise

    def get_json(self, key, limit=100_000_000):
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        stream = response['Body']
        try:
            body = stream.read(limit + 1)
        finally:
            stream.close()
        if len(body) > limit:
            raise ValueError('Catalog exceeds read limit')
        return json.loads(body)

    def inventory(self):
        self.keys = {}
        for page in self.client.get_paginator('list_objects_v2').paginate(Bucket=self.bucket):
            self.keys.update({r['Key']: r['Size'] for r in page.get('Contents', [])})
        self.used = sum(self.keys.values())

    def reserve(self, size):
        if self.used is None:
            self.inventory()
        if self.used + size > self.budget:
            raise ValueError('bucket_budget_exceeded')

    def put(self, key, data, content_type, metadata=None):
        digest = sha(data)
        meta = {'sha256': digest, **(metadata or {})}
        self.reserve(0)
        if key in self.keys:
            self.verify(key, len(data), meta)
            return self.public + '/' + key
        self.reserve(len(data))
        try:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type,
                                   CacheControl='public, max-age=31536000, immutable',
                                   Metadata=meta, IfNoneMatch='*')
        except Exception:
            # Resolve a conditional race/lost response only with matching proof.
            self.verify(key, len(data), meta)
        self.keys[key] = len(data)
        self.used += len(data)
        return self.public + '/' + key

    def verify(self, key, size, metadata):
        head = self.head(key)
        if not head or head['ContentLength'] != size or any(head.get('Metadata', {}).get(k) != v for k, v in metadata.items()):
            raise ValueError('existing_object_identity_conflict')

    def snapshot(self):
        key = PREFIX + '/latest.json'
        head = self.head(key)
        if head is None:
            return {'schema_version': 1, 'properties': {}}, None
        if head.get('Metadata', {}).get('owner') != OWNER:
            raise ValueError('Catalog pointer owned by another application')
        pointer = self.get_json(key, 100_000)
        url = pointer['catalog_url']
        if not url.startswith(self.public + '/' + PREFIX + '/') or not url.endswith('/properties.json'):
            raise ValueError('Catalog pointer outside expected prefix')
        return self.get_json(url[len(self.public) + 1:]), head['ETag']

    def verify_image(self, row):
        for url_field, hash_field, bytes_field in [('source_copy_url', 'optimized_sha256', 'bytes'),
                                                  ('cleaned_url', 'repair_output_sha256', 'repair_output_bytes')]:
            url = row.get(url_field)
            if not url:
                continue
            if not url.startswith(self.public + '/properties/'):
                raise ValueError('Image outside configured delivery base')
            digest = row.get(hash_field)
            if not digest or digest not in url.rsplit('/', 1)[-1]:
                raise ValueError('Image URL and digest disagree')
            self.verify(url[len(self.public) + 1:], int(row[bytes_field]), {'sha256': digest})
        if is_cleaned(row) and not repair_proof_matches(row):
            raise ValueError('Cleaned image lacks matching repair proof')

    def publish(self, updates):
        # Existing cleaned entries are trusted from the owned catalog; new
        # selections are verified against uploaded immutable bytes first.
        for prop in updates.values():
            for row in prop.get('images', []):
                if not row.get('reused_existing'):
                    self.verify_image(row)
        for attempt in range(3):
            current, etag = self.snapshot()
            catalog = merge_catalog(current, updates)
            data = encode(catalog)
            prefix = PREFIX + '/' + sha(data)[:24]
            rows = [{**r, 'property_key': k, 'listing_url': p['listing_url']}
                    for k, p in sorted(catalog['properties'].items()) for r in p.get('images', [])]
            assets = {'properties.json': (data, 'application/json'),
                      'all_images.csv': (csv_bytes(rows), 'text/csv; charset=utf-8'),
                      'cleaned_images.csv': (csv_bytes([r for r in rows if is_cleaned(r)]), 'text/csv; charset=utf-8'),
                      'watermark_candidates.csv': (csv_bytes([r for r in rows if r.get('watermark_candidate')]), 'text/csv; charset=utf-8'),
                      'watermark_remaining.csv': (csv_bytes([r for r in rows if (r.get('watermark_candidate') or r.get('publication_hold')) and not r.get('ready_for_listing')]), 'text/csv; charset=utf-8'),
                      'ready_images.csv': (csv_bytes([r for r in rows if r.get('ready_for_listing')]),'text/csv; charset=utf-8'),
                      'held_images.csv': (csv_bytes([r for r in rows if not r.get('ready_for_listing')]),'text/csv; charset=utf-8')}
            routing=[{'property_key':k,**{field:p.get(field) for field in ('photo_audit_status','photo_audited_count','photo_pending_count',
                     'watermark_agencies','reported_agency_brands','all_photos_agency_branded','suggested_contact_queue','agency_routing_reason')}}
                     for k,p in sorted(catalog['properties'].items())]
            assets['agency_routing.json']=(encode(routing),'application/json')
            summary = {k: catalog[k] for k in ('available_images', 'cleaned_images', 'remaining_candidates', 'watermark_candidates', 'ready_images', 'unavailable_images')}
            assets['summary.json'] = (encode(summary), 'application/json')
            zipped = io.BytesIO()
            with zipfile.ZipFile(zipped, 'w', zipfile.ZIP_DEFLATED) as z:
                for key, prop in sorted(catalog['properties'].items()):
                    z.writestr('properties/' + key.replace('_', '/', 1) + '/images.csv',
                               csv_bytes([{**r, 'property_key': key, 'listing_url': prop['listing_url']} for r in prop.get('images', [])]))
            assets['per_property_triage.zip'] = (zipped.getvalue(), 'application/zip')
            urls = {name: self.put(prefix + '/' + name, body, mime, {'owner': OWNER}) for name, (body, mime) in assets.items()}
            pointer = encode({'schema_version': 1, 'catalog_url': urls['properties.json'], 'assets': urls, **summary})
            self.reserve(len(pointer))
            condition = {'IfMatch': etag} if etag else {'IfNoneMatch': '*'}
            try:
                self.client.put_object(Bucket=self.bucket, Key=PREFIX + '/latest.json', Body=pointer,
                                       ContentType='application/json', CacheControl='public, max-age=60',
                                       Metadata={'owner': OWNER, 'sha256': sha(pointer), 'daily-photo-pipeline': '1'}, **condition)
                return {'catalog_index': self.public + '/' + PREFIX + '/latest.json', 'assets': urls, **summary}
            except Exception as exc:
                if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('PreconditionFailed', '412') or attempt == 2:
                    raise
        raise ValueError('Catalog update retry exhausted')

    def publish_daily(self, folder):
        from pathlib import Path
        names = ['properties.json', 'images.csv', 'watermark_exceptions.csv', 'property_queue.csv',
                 'summary.json', 'listings_with_photos.json', 'listings_with_photos.csv']
        files = {name: (Path(folder) / name).read_bytes() for name in names}
        version = sha(encode({k: sha(v) for k, v in files.items()}))[:24]
        prefix = PREFIX + '/daily/' + version
        assets = {name: self.put(prefix + '/' + name, data,
                                'application/json' if name.endswith('.json') else 'text/csv; charset=utf-8',
                                {'owner': 'graden-daily-photos'}) for name, data in files.items()}
        key = PREFIX + '/daily/latest.json'
        head = self.head(key)
        if head and head.get('Metadata', {}).get('owner') != 'graden-daily-photos':
            raise ValueError('Daily photo pointer owned by another application')
        body = encode({'schema_version': 1, 'assets': assets, 'catalog_index': self.public + '/' + PREFIX + '/latest.json'})
        self.reserve(len(body))
        condition = {'IfMatch': head['ETag']} if head else {'IfNoneMatch': '*'}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType='application/json',
                               CacheControl='public, max-age=60', Metadata={'owner': 'graden-daily-photos', 'sha256': sha(body)}, **condition)
        return {'latest_url': self.public + '/' + key, 'assets': assets}
