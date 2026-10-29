import csv
import io
import json
import os
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import pipeline


def write_csv(path, fields, rows):
    with open(path, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        image = Image.frombytes('RGB', (600, 400), os.urandom(600 * 400 * 3))
        out = io.BytesIO()
        image.save(out, 'PNG')
        self.original = out.getvalue()
        (self.root / 'original.png').write_bytes(self.original)
        self.digest = pipeline.sha(self.original)
        self.manifest = self.root / 'input.csv'
        self.local = self.root / 'local.csv'
        self.clear = self.root / 'clear.csv'
        write_csv(self.manifest, ['property_key', 'listing_url', 'image_number', 'image_url'], [
            {'property_key': 'estitor_629345', 'image_number': '1', 'image_url': 'https://example/1'},
            {'property_key': 'estitor_629345', 'image_number': '2', 'image_url': 'https://example/2'}])
        write_csv(self.local, ['image_url', 'local_path'], [
            {'image_url': 'https://example/1', 'local_path': 'original.png'},
            {'image_url': 'https://example/2', 'local_path': 'original.png'}])
        self.clear_record('cleared', 'human review: fixture')
        self.args = Namespace(manifest=str(self.manifest), local_files=str(self.local), clearances=str(self.clear),
                              output=str(self.root / 'output'), target_bytes=30000, max_bytes=40000, quality_floor=75)

    def tearDown(self):
        self.temp.cleanup()

    def clear_record(self, decision, source):
        write_csv(self.clear, ['original_sha256', 'decision', 'decision_source'], [
            {'original_sha256': self.digest, 'decision': decision, 'decision_source': source}])

    def test_prepare_limits_grouping_and_dedup(self):
        self.assertEqual(pipeline.prepare(self.args), 0)
        entries = pipeline.audit(self.root / 'output/manifest.json', pipeline.clearances(self.clear))
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry['key'], f'properties/estitor/629345/001-{self.digest}.webp')
        self.assertEqual(entry['duplicate_image_urls'], ['https://example/2'])
        self.assertLessEqual(entry['bytes'], 40000)
        self.assertGreaterEqual(entry['quality'], 75)
        self.assertLessEqual(entry['dimensions'][0], 600)
        self.assertLessEqual(entry['dimensions'][1], 400)
        self.assertEqual((self.root / 'original.png').read_bytes(), self.original)
        self.assertTrue((self.root / 'output/properties/estitor/629345/manifest.json').exists())

    def test_gate_and_source_mismatch(self):
        for decision, source in [('pending', 'reviewer'), ('cleared', ''), ('cleared', 'ocr:no_text')]:
            self.clear_record(decision, source)
            self.assertEqual(pipeline.prepare(self.args), 1)
            self.assertFalse(pipeline.audit(self.root / 'output/manifest.json', pipeline.clearances(self.clear)))
        self.clear_record('cleared', 'human review')
        self.assertEqual(pipeline.prepare(self.args), 0)
        (self.root / 'original.png').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'Original bytes changed'):
            pipeline.audit(self.root / 'output/manifest.json', pipeline.clearances(self.clear))

    def test_cache_lookup_without_mapping(self):
        cache = self.root / 'cache'
        cache.mkdir()
        for url in ('https://example/1', 'https://example/2'):
            (cache / (pipeline.sha(url.encode())[:24] + '.image')).write_bytes(self.original)
        self.args.cache_dir = str(cache)
        self.args.local_files = None
        self.assertEqual(pipeline.prepare(self.args), 0)
        entries = pipeline.audit(self.root / 'output/manifest.json', pipeline.clearances(self.clear))
        self.assertEqual(entries[0]['acquisition'], 'url_sha256_cache')

    def test_orientation_and_exif_privacy(self):
        image = Image.new('RGB', (80, 40), 'orange')
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = 'private description'
        exif[34853] = {1: 'N', 2: (1.0, 2.0, 3.0), 3: 'E', 4: (4.0, 5.0, 6.0)}
        original = io.BytesIO()
        image.save(original, format='JPEG', exif=exif)
        encoded, size, original_size, _ = pipeline.optimize(original.getvalue(), 300000, 400000, 75)
        self.assertEqual(original_size, (40, 80))
        self.assertEqual(size, (40, 80))
        with Image.open(io.BytesIO(encoded)) as out:
            self.assertFalse(out.getexif())
            self.assertNotIn('exif', out.info)

    def test_preserves_size_when_under_hard_limit(self):
        encoded, size, original_size, quality = pipeline.optimize(self.original, 1000, 400000, 75)
        self.assertEqual(size, original_size)
        self.assertLessEqual(len(encoded), 400000)
        self.assertGreaterEqual(quality, 75)

    def test_audit_enforces_recorded_size_cap(self):
        self.assertEqual(pipeline.prepare(self.args), 0)
        manifest = self.root / 'output/manifest.json'
        data = json.loads(manifest.read_text())
        data['entries'][0]['max_bytes'] = 1
        manifest.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'exceeds recorded maximum'):
            pipeline.audit(manifest, pipeline.clearances(self.clear))

    def test_upload_budget_and_idempotency_without_network(self):
        self.assertEqual(pipeline.prepare(self.args), 0)
        manifest = self.root / 'output/manifest.json'
        entry = json.loads(manifest.read_text())['entries'][0]
        class ClientError(Exception):
            def __init__(self, code):
                self.response = {'Error': {'Code': code}}
        class Client:
            def __init__(self):
                self.head = None
                self.existing_bytes = 10
                self.puts = []
            def get_paginator(self, name):
                self.assert_name = name
                return self
            def paginate(self, **kwargs):
                return [{'Contents': [{'Size': self.existing_bytes}]}]
            def head_object(self, **kwargs):
                if self.head is None:
                    raise ClientError('404')
                return self.head
            def put_object(self, **kwargs):
                self.puts.append(kwargs)
        client = Client()
        boto = types.ModuleType('boto3')
        boto.client = lambda *args, **kwargs: client
        exceptions = types.ModuleType('botocore.exceptions')
        exceptions.ClientError = ClientError
        exceptions.BotoCoreError = type('BotoCoreError', (Exception,), {})
        botocore = types.ModuleType('botocore')
        env = {n: 'test' for n in ('R2_ENDPOINT_URL', 'R2_BUCKET_NAME', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY')}
        args = Namespace(manifest=str(manifest), clearances=str(self.clear), execute=True,
                         public_base_url='', budget_bytes=entry['bytes'] + 9)
        with patch.dict('sys.modules', {'boto3': boto, 'botocore': botocore, 'botocore.exceptions': exceptions}), patch.dict(os.environ, env):
            with self.assertRaisesRegex(ValueError, 'budget exceeded'):
                pipeline.upload(args)
            self.assertFalse(client.puts)
            args.budget_bytes += 1
            self.assertEqual(pipeline.upload(args), 0)
            self.assertEqual(len(client.puts), 1)
            self.assertEqual(client.puts[0]['Metadata']['sha256'], entry['sha256'])
            self.assertEqual(client.puts[0]['ContentType'], 'image/webp')
            self.assertEqual(client.puts[0]['IfNoneMatch'], '*')
            client.head = {'Metadata': {'sha256': entry['sha256']}, 'ContentLength': entry['bytes']}
            self.assertEqual(pipeline.upload(args), 0)
            self.assertEqual(len(client.puts), 1)
            client.head['Metadata']['sha256'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'Existing object differs'):
                pipeline.upload(args)
            self.assertEqual(len(client.puts), 1)

    def test_dry_run_never_loads_boto3_or_uploads(self):
        self.assertEqual(pipeline.prepare(self.args), 0)
        args = Namespace(manifest=str(self.root / 'output/manifest.json'), clearances=str(self.clear),
                         execute=False, public_base_url='')
        with patch.dict('sys.modules', {'boto3': None}):
            self.assertEqual(pipeline.upload(args), 0)


if __name__ == '__main__':
    unittest.main()
