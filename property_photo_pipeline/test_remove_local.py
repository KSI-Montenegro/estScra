import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
from pipeline import sha
import remove_local as subject


class LocalRemovalTests(unittest.TestCase):
    def test_composites_only_mask_and_pads_to_multiple_of_eight(self):
        original = np.random.default_rng(3).integers(0, 256, (79, 103, 3), dtype=np.uint8)
        before = original.copy()
        mask = np.zeros((79, 103), dtype=np.uint8)
        mask[66:76, 80:100] = 255
        def infer(image, m):
            self.assertEqual(image.shape[0] % 8, 0)
            self.assertEqual(image.shape[1] % 8, 0)
            self.assertTrue(set(np.unique(m)).issubset({0, 1}))
            return np.full_like(image, 42)
        result, box = subject.repair_rgb(original, mask, infer, context=6)
        self.assertTrue(np.array_equal(original, before))
        self.assertTrue(np.array_equal(result[mask == 0], original[mask == 0]))
        self.assertTrue(np.all(result[mask > 0] == 42))
        self.assertEqual(box, [74, 60, 103, 79])

    def test_padding_extends_edge_mask_with_edge_pixels(self):
        original = np.full((13, 11, 3), 127, dtype=np.uint8)
        mask = np.zeros((13, 11), dtype=np.uint8)
        mask[10:13, 9:11] = 255
        def infer(image, padded):
            self.assertEqual(image.shape[:2], (16, 16))
            self.assertTrue(padded[10:, 9:].all())
            self.assertFalse(padded[:10, :9].any())
            return image
        result, _ = subject.repair_rgb(original, mask, infer, context=20)
        self.assertTrue(np.array_equal(result, original))

    def test_unsafe_or_empty_mask_does_not_infer(self):
        original = np.zeros((20, 30, 3), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError, 'empty'):
            subject.repair_rgb(original, np.zeros((20, 30), dtype=np.uint8), lambda *_: self.fail('inference called'))

    def test_source_hash_qc_gating_and_trial(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cache = root / 'cache'
            cache.mkdir()
            image = Image.new('RGB', (200, 200), (40, 60, 90))
            source = io.BytesIO()
            image.save(source, 'PNG')
            original = source.getvalue()
            url = 'https://example.org/original'
            path = cache / (sha(url.encode())[:24] + '.image')
            path.write_bytes(original)
            row = {'property_key': 'patuljak_123', 'image_number': '1', 'image_url': url,
                   'source_sha256': sha(original), 'triage_group': 'known_logo_template_candidate'}
            template = np.asarray(Image.open(Path(__file__).parent / 'reference_assets/patuljak_mascot.png').convert('RGB'))
            def mask_builder(row, rgb):
                mask = np.zeros(rgb.shape[:2], dtype=np.uint8)
                mask[185:195, 185:195] = 255
                return mask, {'eligible': True, 'overlay_box_original_pixels': [180, 180, 199, 199]}
            def infer(rgb, mask):
                return np.full_like(rgb, 100)
            called = []
            def ocr(rgb, box):
                called.append(box)
                return {'brand_detected': False}
            result = subject.process_row(row, cache, root / 'out', mask_builder, infer, template, 'model', 'config', ocr=ocr)
            self.assertEqual(result['status'], 'processed', result.get('error'))
            self.assertEqual(result['certification'], 'not_certified')
            self.assertEqual(called, [[176, 176, 200, 200]])
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(result['source_oriented_dimensions'], result['output_dimensions'])
            queued = subject.process_row(row, cache, root / 'out', mask_builder, infer, template, 'model', 'config',
                                         ocr=lambda *_: {'brand_detected': True})
            self.assertEqual(queued['status'], 'queued')
            self.assertNotIn('output_path', queued)
            row['triage_group'] = 'known_brand_text_candidate'
            queued = subject.process_row(row, cache, root / 'out', lambda *_: (None, {'eligible': False}), infer, template, 'model', 'config', ocr=ocr)
            self.assertEqual(queued['status'], 'queued')
            row['triage_group'] = 'known_logo_template_candidate'
            path.write_bytes(b'changed')
            bad = subject.process_row(row, cache, root / 'out', mask_builder, infer, template, 'model', 'config', ocr=ocr)
            self.assertEqual(bad['status'], 'error')
            self.assertIn('SHA256 mismatch', bad['error'])

    def test_upload_guard_and_status_are_not_certification(self):
        class FakeS3:
            def __init__(self):
                self.objects = {}
            def get_paginator(self, _):
                return self
            def paginate(self, **kwargs):
                return [{'Contents': [{'Key': k, 'Size': len(v['Body'])} for k, v in self.objects.items()]}]
            def put_object(self, **kwargs):
                self.objects[kwargs['Key']] = kwargs
            def head_object(self, Bucket, Key):
                obj = self.objects[Key]
                return {'ContentLength': len(obj['Body']), 'Metadata': obj['Metadata']}
        with tempfile.TemporaryDirectory() as d:
            content = b'fake-encoded-payload'
            path = Path(d) / 'result.webp'
            path.write_bytes(content)
            row = {'status': 'processed', 'output_path': str(path), 'output_sha256': sha(content),
                   'source_sha256': 'a' * 64, 'output_bytes': len(content), 'key': 'properties/x/1/cleaned/a.webp'}
            client = FakeS3()
            with self.assertRaisesRegex(ValueError, 'budget'):
                subject.upload_processed([row], 'bucket', client, len(content)-1, '')
            self.assertFalse(client.objects)
            subject.upload_processed([row], 'bucket', client, 1000, 'https://example.test')
            obj = client.objects[row['key']]
            self.assertEqual(obj['Metadata']['status'], 'processed')
            self.assertEqual(obj['IfNoneMatch'], '*')
            self.assertEqual(row['upload_status'], 'uploaded')
            subject.upload_processed([row], 'bucket', client, 1000, 'https://example.test')
            self.assertEqual(row['upload_status'], 'existing')
            obj['Metadata']['sha256'] = 'wrong'
            with self.assertRaisesRegex(ValueError, 'conflicts'):
                subject.upload_processed([row], 'bucket', client, 1000, '')

    def test_parallel_upload_workers_and_failure_isolation(self):
        class FakeS3:
            def __init__(self):
                self.lock = threading.Lock()
                self.active = self.peak = 0
            def get_paginator(self, _):
                return self
            def paginate(self, **kwargs):
                return [{'Contents': []}]
            def put_object(self, **kwargs):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                try:
                    time.sleep(.02)
                    if kwargs['Key'].endswith('bad'):
                        exc = RuntimeError('failed')
                        exc.response = {'Error': {'Code': 'PreconditionFailed'}}
                        raise exc
                finally:
                    with self.lock:
                        self.active -= 1
            def head_object(self, **kwargs):
                exc = RuntimeError('object missing')
                exc.response = {'Error': {'Code': '404'}}
                raise exc
        with tempfile.TemporaryDirectory() as d:
            file = Path(d) / 'ready'
            file.write_bytes(b'x')
            rows = [{'status':'processed', 'output_path':str(file), 'output_sha256':sha(b'x'),
                     'output_bytes':1, 'source_sha256':'a'*64, 'key':f'object-{i}'} for i in range(3)]
            rows[2]['key'] = 'object-bad'
            client, callbacks = FakeS3(), []
            subject.upload_processed(rows, 'bucket', client, 1000, '', workers=2, on_result=callbacks.append)
            self.assertEqual(client.peak, 2)
            self.assertEqual([r['upload_status'] for r in rows], ['uploaded', 'uploaded', 'error'])
            self.assertEqual(len(callbacks), 3)
            self.assertEqual(rows[2]['uploaded_url'], '')

    def test_text_shape_scores_fixed_coordinates_without_certification(self):
        from text_shape_metric import shape_scores
        image = np.random.default_rng(17).integers(0, 256, (500, 666, 3), dtype=np.uint8)
        templates = {'word': image[441:492, 376:535].copy(), 'dotme': image[447:490, 598:665].copy()}
        scores = shape_scores(image, [530, 400, 605, 493], templates)
        self.assertGreater(scores['word']['score'], .99)
        self.assertEqual(scores['word']['box'], [376, 441, 535, 492])
        self.assertGreater(scores['dotme']['score'], .99)
        blank = np.zeros_like(image)
        self.assertIsNone(shape_scores(blank, [530, 400, 605, 493], templates)['word']['score'])
        with self.assertRaises(ValueError):
            shape_scores(image, [10, 10, 20, 100], templates)

    def test_mask_ineligible_does_not_infer(self):
        with tempfile.TemporaryDirectory() as d:
            cache = Path(d)
            img = io.BytesIO()
            Image.new('RGB', (100, 100), 'white').save(img, 'PNG')
            data, url = img.getvalue(), 'https://example.org/blocked'
            (cache / (sha(url.encode())[:24] + '.image')).write_bytes(data)
            row = {'property_key':'patuljak_1', 'image_number':'1', 'image_url':url, 'source_sha256':sha(data), 'triage_group':'known_logo_template_candidate'}
            result = subject.process_row(row, cache, cache, lambda *_: (None, {'eligible':False}),
                                         lambda *_: self.fail('inference called'), None, 'model', 'config')
            self.assertEqual(result['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
