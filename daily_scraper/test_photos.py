import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent))
import process_photos as p
from photo_catalog import R2Store, merge_catalog, sha, encode, OWNER, PREFIX
from photo_queue import enqueue, prepare
from PIL import Image

BASE = 'https://cdn.example.com'


def image_bytes(color='blue'):
    out = io.BytesIO()
    Image.new('RGB', (320, 240), color).save(out, 'PNG')
    return out.getvalue()


class Missing(Exception):
    response = {'Error': {'Code': '404'}}


class MemoryS3:
    def __init__(self): self.objects, self.puts = {}, []
    def head_object(self, Bucket, Key):
        if Key not in self.objects: raise Missing()
        obj = self.objects[Key]
        return {'ContentLength': len(obj['Body']), 'Metadata': obj['Metadata'], 'ETag': sha(obj['Body'])}
    def get_object(self, Bucket, Key): return {'Body': io.BytesIO(self.objects[Key]['Body'])}
    def put_object(self, **kw):
        key = kw['Key']
        if kw.get('IfNoneMatch') == '*' and key in self.objects:
            raise ValueError('Conditional conflict')
        if kw.get('IfMatch') and sha(self.objects[key]['Body']) != kw['IfMatch']:
            raise ValueError('ETag conflict')
        self.objects[key] = {'Body': kw['Body'], 'Metadata': kw['Metadata']}
        self.puts.append(key)
    def get_paginator(self, name): return self
    def paginate(self, **kw):
        yield {'Contents': [{'Key': k, 'Size': len(v['Body'])} for k, v in self.objects.items()]}


class FakeDownloader:
    api_requests = 0
    def __init__(self, root, contents): self.cache, self.contents = root, contents
    def get(self, url):
        data = self.contents[url]
        self.cache.mkdir(exist_ok=True)
        (self.cache / (sha(url.encode())[:24] + '.image')).write_bytes(data)
        return data


class FakeStore:
    def __init__(self, catalog=None): self.catalog, self.puts = catalog or {'properties': {}}, []
    def snapshot(self): return self.catalog, 'etag'
    def put(self, key, data, mime, metadata=None):
        self.puts.append(key)
        return BASE + '/' + key


class Detector:
    template = None
    def __init__(self, result): self.result, self.calls = result, 0
    def analyze(self, data):
        self.calls += 1
        return {'result': self.result, 'reason': 'fixture', 'score': 1, 'box': [1, 2, 3, 4], 'hits': []}


class PhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.urls = ['https://patuljak.me/a.jpg', 'https://patuljak.me/b.jpg']
        self.record = {'property_key': 'patuljak_123', 'listing_url': 'https://patuljak.me/oglas/stan--123', 'image_urls': self.urls}
    def tearDown(self): self.temp.cleanup()
    def worker(self, result='sample_passed', catalog=None, **kw):
        state = {'schema_version': 1, 'properties': {}}
        enqueue(state, [self.record])
        return p.PhotoWorker(state, self.root / 'state.json', self.root / 'out', FakeStore(catalog), Detector(result),
                             FakeDownloader(self.root / 'cache', dict(zip(self.urls, [image_bytes(), image_bytes('red')]))),
                             self.root / 'model.pt', **kw)

    def test_negative_sample_checks_only_one_image_then_uploads_gallery(self):
        w = self.worker()
        prop = w.state['properties']['patuljak_123']
        w.process(prop)
        self.assertEqual(w.detector.calls, 1)
        self.assertEqual(prop['sample']['images_sampled'], 1)
        self.assertEqual(prop['phase'], 'complete')
        self.assertEqual(len(w.store.puts), 2)
        self.assertTrue(all(r['watermark_status'] == 'sample_passed' for r in prop['images'].values()))
        self.assertTrue(all(r['bytes'] <= 400000 for r in prop['images'].values()))

    def test_resume_does_not_reencode_uploaded_images_or_resample(self):
        w = self.worker(); prop = w.state['properties']['patuljak_123']
        w.process(prop)
        puts, calls = len(w.store.puts), w.detector.calls
        prop['phase'] = 'pending'  # Simulate interruption before catalog publication.
        w.process(prop)
        self.assertEqual((len(w.store.puts), w.detector.calls), (puts, calls))
        self.assertEqual(len(w.updates['patuljak_123']['images']), 2)

    def test_unknown_mark_holds_gallery_and_never_calls_inpainting(self):
        w = self.worker('needs_review'); prop = w.state['properties']['patuljak_123']
        with patch.object(w, 'repair', return_value=(None, 'unsupported_or_uncertain_mask')) as repair:
            w.process(prop)
            self.assertEqual(repair.call_count, 2)
        self.assertEqual(prop['phase'], 'needs_review')
        self.assertTrue(all(not r['ready_for_listing'] for r in prop['images'].values()))

    def test_existing_cleaned_images_never_sampled_or_downgraded(self):
        cleaned = {'image_number': 1, 'selected_version': 'cleaned', 'cleaned_url': BASE + '/clean.webp',
                   'active_url': BASE + '/clean.webp', 'source_copy_url': BASE + '/source.webp'}
        w = self.worker(catalog={'properties': {'patuljak_123': {'images': [cleaned]}}})
        w.state['properties']['patuljak_123']['input_images'] = w.state['properties']['patuljak_123']['input_images'][:1]
        w.process(w.state['properties']['patuljak_123'])
        self.assertEqual(w.detector.calls, 0)
        self.assertEqual(w.store.puts, [])
        self.assertEqual(w.updates['patuljak_123']['images'][0]['active_url'], cleaned['active_url'])

    def test_catalog_merge_preserves_cleaned_and_adds_new_properties(self):
        cleaned = {'image_number': 1, 'selected_version': 'cleaned', 'cleaned_url': 'clean', 'active_url': 'clean', 'source_sha256': 'original'}
        current = {'properties': {'patuljak_123': {'listing_url': 'old', 'images': [cleaned], 'unavailable': []}}}
        updated = {'patuljak_123': {'listing_url': 'old', 'images': [{'image_number': 1, 'selected_version': 'source', 'source_sha256': 'different'}]},
                   'estitor_456': {'listing_url': 'new', 'images': [{'image_number': 1, 'selected_version': 'source', 'active_url': 'source'}]}}
        merged = merge_catalog(current, updated)
        self.assertEqual(merged['properties']['patuljak_123']['images'][0]['active_url'], 'clean')
        self.assertEqual(merged['cleaned_images'], 1)
        self.assertEqual(merged['available_images'], 2)
        self.assertNotIn('ready_for_listing', cleaned)  # Input snapshot remains immutable.

    def test_object_hash_collision_and_budget_refuse_writes(self):
        client = MemoryS3(); store = R2Store(client, 'bucket', BASE, budget=3)
        with self.assertRaisesRegex(ValueError, 'budget'):
            store.put('a', b'1234', 'image/webp')
        self.assertFalse(client.puts)
        store = R2Store(client, 'bucket', BASE)
        store.put('a', b'abc', 'image/webp')
        with self.assertRaisesRegex(ValueError, 'identity_conflict'):
            store.put('a', b'xyz', 'image/webp')
        self.assertEqual(client.puts, ['a'])

    def test_shared_catalog_publication_and_retry_are_idempotent(self):
        client = MemoryS3(); store = R2Store(client, 'bucket', BASE)
        data = b'encoded'; digest = sha(data); key = 'properties/estitor/123/source/001-' + digest + '.webp'
        url = store.put(key, data, 'image/webp')
        row = {'image_number': 1, 'source_copy_url': url, 'active_url': url, 'selected_version': 'source',
               'optimized_sha256': digest, 'source_sha256': 'raw', 'bytes': len(data), 'ready_for_listing': True}
        updates = {'estitor_123': {'listing_url': 'listing', 'images': [row]}}
        store.publish(updates); store.publish(updates)
        catalog, _ = store.snapshot()
        self.assertEqual(catalog['available_images'], 1)
        self.assertEqual(len(catalog['properties']['estitor_123']['images']), 1)
        self.assertEqual(client.puts.count(key), 1)

    def test_unowned_pointer_is_never_replaced(self):
        client = MemoryS3()
        client.objects[PREFIX + '/latest.json'] = {'Body': b'{}', 'Metadata': {'owner': 'another-app'}}
        with self.assertRaisesRegex(ValueError, 'another application'):
            R2Store(client, 'bucket', BASE).snapshot()
        self.assertFalse(client.puts)

    def test_cleaned_selection_requires_source_and_qc_proof(self):
        client = MemoryS3(); store = R2Store(client, 'bucket', BASE)
        data = b'source'; digest = sha(data)
        source_url = store.put('properties/estitor/123/source/001-' + digest + '.webp', data, 'image/webp')
        repaired = b'cleaned'; repaired_hash = sha(repaired)
        cleaned_url = store.put('properties/estitor/123/cleaned/001-' + repaired_hash + '.webp', repaired, 'image/webp')
        row = {'source_copy_url': source_url, 'optimized_sha256': digest, 'bytes': len(data), 'source_sha256': 'raw',
               'selected_version': 'cleaned', 'cleaned_url': cleaned_url, 'repair_output_sha256': repaired_hash,
               'repair_output_bytes': len(repaired)}
        with self.assertRaisesRegex(ValueError, 'repair proof'):
            store.verify_image(row)
        row.update(repair_input_sha256='raw', repair_qc='mascot_and_ocr_only_pass_text_shape_unvalidated',
                   repair_policy_id='patuljak-footer-stable-colors-v2-filled-word-bands', outside_mask_unchanged_before_encoding=True)
        store.verify_image(row)

    def test_existing_sampled_sources_are_reused_without_downloading_gallery(self):
        raw = image_bytes()
        base = {'image_number': 2, 'source_copy_url': BASE + '/existing.webp', 'active_url': BASE + '/existing.webp',
                'selected_version': 'source', 'source_sha256': 'old', 'watermark_candidate': False}
        w = self.worker(catalog={'properties': {'patuljak_123': {'images': [base]}}})
        prop = w.state['properties']['patuljak_123']
        with patch.object(w.downloader, 'get', wraps=w.downloader.get) as get:
            w.process(prop)
            self.assertEqual(get.call_count, 2)  # First image is sampled and encoded; second reuses R2.
        self.assertEqual(w.counts['reused_source'], 1)
        self.assertEqual(prop['images']['2']['active_url'], BASE + '/existing.webp')

    def test_new_daily_inputs_are_prioritized_over_backfill(self):
        state = {'properties': {}}
        enqueue(state, [self.record], 'backfill')
        enqueue(state, [self.record], 'daily')
        self.assertEqual(state['properties']['patuljak_123']['origin'], 'daily')

    def test_enriched_listing_uses_only_ready_r2_urls_and_actual_sample(self):
        folder = self.root / 'exports/run'; folder.mkdir(parents=True)
        source = folder / 'listings.json'; source.write_text(json.dumps([self.record]))
        state = {'properties': {'patuljak_123': {'phase': 'needs_review',
            'sample': {'result': 'watermark_candidate', 'sampled_url': self.urls[0]},
            'images': {'1': {'image_number': 1, 'active_url': BASE + '/clean.webp', 'ready_for_listing': True},
                       '2': {'image_number': 2, 'active_url': BASE + '/held.webp', 'ready_for_listing': False}}}}}
        rows = p.enrich_exports(folder.parent, [], state, self.root / 'out', BASE + '/latest.json')
        self.assertEqual(rows[0]['image_urls'], [BASE + '/clean.webp'])
        self.assertEqual(rows[0]['source_image_urls'], self.urls)
        self.assertEqual(rows[0]['watermark_sample_result'], 'watermark_candidate')
        self.assertEqual(rows[0]['held_image_count'], 1)

    def test_known_candidate_remains_held_after_a_negative_sample(self):
        raw = image_bytes()
        base = {'image_number': 1, 'source_copy_url': BASE + '/existing.webp', 'active_url': BASE + '/existing.webp',
                'selected_version': 'source', 'source_sha256': sha(raw), 'watermark_candidate': True}
        w = self.worker(catalog={'properties': {'patuljak_123': {'images': [base]}}})
        with patch.object(w, 'repair', return_value=(None, 'unsupported_or_uncertain_mask')):
            w.process(w.state['properties']['patuljak_123'])
        self.assertFalse(w.state['properties']['patuljak_123']['images']['1']['ready_for_listing'])

    def test_daily_index_refuses_another_owners_pointer(self):
        client = MemoryS3(); store = R2Store(client, 'bucket', BASE)
        folder = self.root / 'daily'; folder.mkdir()
        for name in ('properties.json', 'images.csv', 'watermark_exceptions.csv', 'property_queue.csv',
                     'summary.json', 'listings_with_photos.json', 'listings_with_photos.csv'):
            (folder / name).write_text('{}')
        key = PREFIX + '/daily/latest.json'
        client.objects[key] = {'Body': b'{}', 'Metadata': {'owner': 'another-app'}}
        with self.assertRaisesRegex(ValueError, 'another application'):
            store.publish_daily(folder)
        self.assertEqual(client.objects[key]['Metadata']['owner'], 'another-app')

    def test_secret_free_queue_survives_and_skips_completed_inputs(self):
        exports = self.root / 'exports/run'; exports.mkdir(parents=True)
        (exports / 'listings.json').write_text(json.dumps([{**self.record, 'phone_e164': 'private-contact'}]))
        missing = self.root / 'missing.json'
        state = prepare(self.root / 'state.json', exports.parent, legacy_manifest='', backfill=missing)
        state['properties']['patuljak_123']['phase'] = 'complete'
        (self.root / 'state.json').write_text(json.dumps(state))
        resumed = prepare(self.root / 'state.json', exports.parent, legacy_manifest='', backfill=missing)
        self.assertEqual(resumed['properties']['patuljak_123']['phase'], 'complete')
        self.assertNotIn('private-contact', json.dumps(resumed))

    def test_download_whitelist_rejects_private_hosts_and_credentials(self):
        self.assertFalse(p.safe_url('https://127.0.0.1/a', p.HOSTS))
        self.assertFalse(p.safe_url('https://user:pass@patuljak.me/a', p.HOSTS))
        self.assertFalse(p.safe_url('http://patuljak.me/a', p.HOSTS))
        self.assertTrue(p.safe_url(self.urls[0], p.HOSTS))

    def test_blocked_raw_proxy_image_uses_browser_binary_and_reuses_route(self):
        downloader = p.Downloader(self.root / 'cache', 'cdn.example.com', key='test-key')
        downloader.opener = MagicMock()
        downloader.opener.open.side_effect = HTTPError(self.urls[0], 403, 'blocked', {}, None)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'<html>CloudFront blocked</html>'
        api = MagicMock(); api.open.return_value = response
        raw = image_bytes()
        with patch.object(p, 'build_opener', return_value=api), \
             patch.object(downloader.browser, 'get_image', return_value=(raw, self.urls[0], 'browser')) as browser, \
             patch.object(p.time, 'sleep'):
            self.assertEqual(downloader.get(self.urls[0]), raw)
            self.assertEqual(downloader.get(self.urls[1]), raw)
            self.assertEqual(browser.call_count, 2)
        self.assertEqual(downloader.opener.open.call_count, 1)
        self.assertEqual(downloader.api_requests, 3)

    def test_request_limit_does_not_exhaust_per_image_failure_retries(self):
        w = self.worker()
        with patch.object(w.downloader, 'get', side_effect=p.PhotoFailure('image_api_request_limit')):
            w.process(w.state['properties']['patuljak_123'])
        prop = w.state['properties']['patuljak_123']
        self.assertEqual(prop['failures'], {'1': 0, '2': 0})
        self.assertEqual(prop['phase'], 'pending')
        self.assertEqual(prop['sample']['images_sampled'], 0)

    def test_existing_gallery_samples_its_verified_r2_source_copy(self):
        raw = image_bytes()
        base = {'image_number': 1, 'source_copy_url': BASE + '/source.webp', 'active_url': BASE + '/source.webp',
                'selected_version': 'source', 'source_sha256': sha(raw), 'optimized_sha256': sha(raw),
                'watermark_candidate': False}
        w = self.worker(catalog={'properties': {'patuljak_123': {'images': [base]}}})
        prop = w.state['properties']['patuljak_123']; prop['input_images'] = prop['input_images'][:1]
        w.downloader.contents[BASE + '/source.webp'] = raw
        with patch.object(w.downloader, 'get', wraps=w.downloader.get) as get:
            w.process(prop)
            self.assertEqual(get.call_count, 1)
            self.assertEqual(get.call_args.args[0], BASE + '/source.webp')
        self.assertEqual(prop['phase'], 'complete')

    def test_broken_cover_falls_back_to_one_available_gallery_photo(self):
        w = self.worker()
        original = w.downloader.get
        def fetch(url):
            if url == self.urls[0]: raise p.PhotoFailure('image_http_404')
            return original(url)
        with patch.object(w.downloader, 'get', side_effect=fetch):
            w.process(w.state['properties']['patuljak_123'])
        prop = w.state['properties']['patuljak_123']
        self.assertEqual(prop['sample']['image_number'], 2)
        self.assertEqual(prop['sample']['images_sampled'], 1)
        self.assertEqual(w.detector.calls, 1)
        self.assertTrue(prop['images']['2']['ready_for_listing'])


if __name__ == '__main__': unittest.main()
