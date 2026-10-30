import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from pipeline import sha
from stage_cached import stage
import upload_staged as subject


class Error(Exception):
    def __init__(self, code):
        self.response = {'Error': {'Code': code}}


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.used = 5
        self.puts = []
    def get_paginator(self, name):
        return self
    def paginate(self, **kwargs):
        return [{'Contents': [{'Size': self.used}]}]
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Error('404')
        return self.objects[Key]
    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.objects[kwargs['Key']] = {'ContentLength': len(kwargs['Body']), 'Metadata': kwargs['Metadata']}


class UploadStagedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache, self.staging = self.root / 'cache', self.root / 'staging'
        self.cache.mkdir()
        img = io.BytesIO()
        Image.new('RGB', (64, 40), 'blue').save(img, 'PNG')
        raw = img.getvalue()
        self.url = 'https://example.test/image.png'
        (self.cache / (sha(self.url.encode())[:24] + '.image')).write_bytes(raw)
        item = {'property_key': 'estitor_629345', 'image_number': '1', 'image_url': self.url,
                'source_sha256': sha(raw), 'status': 'known_brand_text_candidate'}
        record = stage(item, self.cache, self.staging)
        fields = ['property_key', 'image_number', 'image_url', 'source_sha256', 'ocr_status',
                  'stage_status', 'optimized_bytes', 'optimized_sha256', 'staged_path']
        with (self.staging / 'staging_inventory.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerow(record)
        self.original = raw
        self.record = record
        self.output = self.root / 'output'
    def tearDown(self):
        self.temp.cleanup()
    def rows(self):
        return subject.load_inventory(self.staging, self.cache, 'https://cdn.example.test')
    def test_unverified_upload_metadata_budget_and_resume(self):
        rows = self.rows()
        row = rows[0]
        self.assertEqual(row['upload_status'], 'planned')
        self.assertEqual(row['uploaded_url'], '')
        self.assertTrue(row['expected_public_url'].startswith('https://cdn.example.test/'))
        self.assertIn('/source/001-', row['key'])
        fake = FakeS3()
        with self.assertRaisesRegex(ValueError, 'budget exceeded'):
            subject.perform_uploads(rows, fake, 'bucket', row['optimized_bytes'] + 4, 2, self.output / 'checkpoint.jsonl')
        self.assertFalse(fake.puts)
        subject.perform_uploads(rows, fake, 'bucket', row['optimized_bytes'] + 5, 2, self.output / 'checkpoint.jsonl')
        self.assertEqual(row['upload_status'], 'uploaded')
        self.assertEqual(row['uploaded_url'], row['expected_public_url'])
        put = fake.puts[0]
        self.assertEqual(put['Metadata']['watermark-status'], 'unverified')
        self.assertEqual(put['Metadata']['ocr-status'], 'known_brand_text_candidate')
        self.assertEqual(put['Metadata']['source-sha256'], sha(self.original))
        self.assertEqual(put['ContentType'], 'image/webp')
        self.assertEqual(put['IfNoneMatch'], '*')
        subject.export(rows, self.output)
        saved = list(csv.DictReader(io.StringIO((self.output / 'inventory.csv').read_text())))[0]
        self.assertEqual(saved['uploaded_url'], row['expected_public_url'])
        self.assertEqual(saved['watermark_status'], 'unverified')
        rerun = self.rows()
        rerun[0]['ocr_status'] = 'other_text_detected'
        subject.perform_uploads(rerun, fake, 'bucket', 100000, 2, self.output / 'checkpoint.jsonl')
        self.assertEqual(rerun[0]['upload_status'], 'existing')
        self.assertEqual(len(fake.puts), 1)
        fake.objects[row['key']]['Metadata']['sha256'] = 'different'
        with self.assertRaisesRegex(ValueError, 'preflight failed'):
            subject.perform_uploads(self.rows(), fake, 'bucket', 100000, 2, self.output / 'checkpoint.jsonl')
        self.assertEqual(len(fake.puts), 1)
    def test_changed_original_and_optimized_fail_before_upload(self):
        staged = self.staging / self.record['staged_path']
        staged.write_bytes(b'changed')
        self.assertEqual(self.rows()[0]['upload_status'], 'error')
        staged.write_bytes(b'x' * 400001)
        self.assertEqual(self.rows()[0]['upload_status'], 'error')
        (self.cache / (sha(self.url.encode())[:24] + '.image')).write_bytes(b'changed')
        self.assertIn('original bytes', self.rows()[0]['error'])
    def test_rejects_oversized_even_with_matching_record_hash(self):
        data = b'x' * 400001
        staged = self.staging / self.record['staged_path']
        staged.write_bytes(data)
        record_path = self.staging / 'records/estitor_629345/001.json'
        record = json.loads(record_path.read_text())
        record.update(optimized_bytes=len(data), optimized_sha256=sha(data))
        record_path.write_text(json.dumps(record))
        inventory = self.staging / 'staging_inventory.csv'
        with inventory.open(newline='') as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames
            row = next(reader)
        row.update(optimized_bytes=len(data), optimized_sha256=sha(data))
        with inventory.open('w', newline='') as f:
            writer = csv.DictWriter(f, fields)
            writer.writeheader()
            writer.writerow(row)
        self.assertIn('exceeds 400000', self.rows()[0]['error'])

    def test_export_planned_has_no_uploaded_url(self):
        rows = self.rows()
        subject.export(rows, self.output)
        entry = list(csv.DictReader(io.StringIO((self.output / 'inventory.csv').read_text())))[0]
        self.assertEqual(entry['upload_status'], 'planned')
        self.assertEqual(entry['uploaded_url'], '')
        self.assertNotEqual(entry['expected_public_url'], '')
        manifest = json.loads((self.output / 'by_property/estitor/629345/inventory.json').read_text())
        self.assertEqual(manifest['entries'][0]['watermark_status'], 'unverified')


if __name__ == '__main__':
    unittest.main()
