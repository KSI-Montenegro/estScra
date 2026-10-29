import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import stage_cached as s


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / 'cache'; self.cache.mkdir()
        self.output = self.root / 'stage'
        buf = io.BytesIO(); Image.new('RGB', (160, 100), 'green').save(buf, 'JPEG')
        self.raw = buf.getvalue()
        self.row = dict(property_key='estitor_123', image_number='1', image_url='https://example.com/a.jpg',
                        source_sha256=s.sha(self.raw), status='no_text_detected')
        self.path = self.cache / (s.sha(self.row['image_url'].encode())[:24] + '.image')
        self.path.write_bytes(self.raw)

    def tearDown(self):
        self.temp.cleanup()

    def test_encoding_does_not_clear_no_text_and_original_is_unchanged(self):
        result = s.stage(self.row, self.cache, self.output)
        self.assertFalse(result['publishable'])
        self.assertEqual(result['clearance_status'], 'pending_verified_policy_or_removal_qc')
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(result['dimensions'], [160, 100])
        self.assertLessEqual(result['optimized_bytes'], 400000)
        self.assertTrue(result['staged_path'].startswith('unverified/properties/estitor/123/'))

    def test_changed_captured_bytes_are_rejected(self):
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed since OCR'):
            s.stage(self.row, self.cache, self.output)

    def test_resumes_without_reencoding_and_exports_pending_state(self):
        first = s.stage(self.row, self.cache, self.output)
        with patch.object(s, 'optimize', side_effect=AssertionError('Must reuse verified encoding')):
            second = s.stage(self.row, self.cache, self.output)
        self.assertEqual(first['optimized_sha256'], second['optimized_sha256'])
        summary = s.export(self.output, {('estitor_123', '1'): second}, True)
        self.assertEqual(summary['publishable_images'], 0)
        self.assertEqual(summary['uploaded_images'], 0)
        self.assertEqual(json.loads((self.output / 'summary.json').read_text())['stage_statuses'], {'encoded': 1})


if __name__ == '__main__':
    unittest.main()
