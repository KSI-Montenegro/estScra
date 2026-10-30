import hashlib
import io
import json
import unittest
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import match_known_logo as matcher


class TemplateMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).resolve().parent
        cls.config = json.loads((cls.root / 'reference_assets/patuljak_mascot.json').read_text())
        cls.ref_path = cls.root.parent / cls.config['reference_image']
        cls.original_hash = hashlib.sha256(cls.ref_path.read_bytes()).hexdigest() if cls.ref_path.exists() else None
        cls.template = np.asarray(Image.open(cls.root / 'reference_assets' / cls.config['template_file']).convert('RGB'))

    def test_reference_hash_and_coordinates(self):
        if not self.ref_path.exists():
            self.skipTest('Original reference is not included in the repository')
        image = np.asarray(Image.open(self.ref_path).convert('RGB'))
        score, bbox, scale = matcher.match_image(image, self.template)
        self.assertEqual(self.original_hash, self.config['reference_sha256'])
        self.assertEqual(bbox, self.config['bbox_xyxy'])
        self.assertEqual(scale, 1.0)
        self.assertGreater(score, .99)
        self.assertEqual(hashlib.sha256(self.ref_path.read_bytes()).hexdigest(), self.original_hash)

    def test_synthetic_scales_and_absent(self):
        for scale in (.75, 1.5):
            image = np.random.default_rng(22).integers(0, 256, (420, 400, 3), dtype=np.uint8)
            w, h = round(self.template.shape[1] * scale), round(self.template.shape[0] * scale)
            resized = cv2.resize(self.template, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            x, y = 100, 275
            image[y:y+h, x:x+w] = resized
            score, bbox, found_scale = matcher.match_image(image, self.template)
            self.assertGreater(score, .99)
            self.assertEqual(bbox, [x, y, x+w, y+h])
            self.assertEqual(found_scale, scale)
        empty = np.random.default_rng(23).integers(0, 256, (420, 400, 3), dtype=np.uint8)
        self.assertLess(matcher.match_image(empty, self.template)[0], .85)

    def test_missing_captured_bytes_never_cleared(self):
        row = {'property_key': 'patuljak_1', 'image_number': '1', 'image_url': 'https://x', 'source_sha256': '', 'status': 'error'}
        result = matcher.scan_one(row, Path('/tmp'), self.template, .85)
        self.assertEqual(result['match_status'], 'unavailable')
        self.assertEqual(result['score'], '')


if __name__ == '__main__':
    unittest.main()
