"""Known central stamps, negative scene controls, and fail-closed delivery checks."""
import io
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image

from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "property_photo_pipeline"))
import portal_watermarks as portal
from remove_local import repair_rgb


def scene(width=1200, height=1600):
    y, x = np.mgrid[:height, :width]
    grey = 65 + 28 * np.sin(x / 120) + 20 * np.cos(y / 130)
    rgb = np.stack([grey + 4, grey + 10, grey + 16], axis=2).astype(np.uint8)
    cv2.rectangle(rgb, (40, height - 110), (330, height - 25), (220, 220, 220), -1)
    cv2.putText(rgb, 'PG AB123', (50, height - 50), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    return rgb


def stamp(rgb, brand, offset=None):
    height, width = rgb.shape[:2]
    x, y, scale = portal.geometry(brand, width, height)
    if offset is not None:
        x, y = offset
    ref = portal.references()[brand]
    alpha = cv2.resize(ref, (round(ref.shape[1] * scale), round(ref.shape[0] * scale))) * .3
    h, w = alpha.shape
    out = rgb.copy()
    original = out[y:y+h, x:x+w]
    out[y:y+h, x:x+w] = (original * (1 - alpha[..., None]) + 255 * alpha[..., None]).round().astype(np.uint8)
    return out


class PortalTests(unittest.TestCase):
    def test_known_word_shapes_located_and_masks_bounded_for_both_sources(self):
        for brand in portal.WORD_BOXES:
            with self.subTest(brand=brand):
                rgb = stamp(scene(), brand)
                mask, meta = portal.build_mask(rgb, brand)
                self.assertTrue(meta['eligible'], meta)
                self.assertLess(meta['mask_fraction'], .10)
                self.assertEqual([row['brand'] for row in meta['stamps']], [brand])
                self.assertEqual(mask.dtype, np.uint8)
                # The vehicle's plate and the surrounding scene are outside the repair.
                self.assertFalse(np.any(mask[-110:-25, 40:330]))

    def test_scene_text_and_unrecognised_corner_stamp_do_not_authorize_edits(self):
        for brand in portal.WORD_BOXES:
            for rgb in (scene(), stamp(scene(), brand, offset=(20, 20))):
                mask, meta = portal.build_mask(rgb, brand)
                self.assertIsNone(mask)
                self.assertFalse(meta['eligible'])

    def test_random_texture_is_not_a_portal_stamp(self):
        rng = np.random.default_rng(420)
        rgb = rng.integers(0, 256, (900, 1200, 3), dtype=np.uint8)
        for brand in portal.WORD_BOXES:
            mask, meta = portal.build_mask(rgb, brand)
            self.assertIsNone(mask)
            self.assertFalse(meta['eligible'])

    def test_small_unvalidated_geometry_held(self):
        mask, meta = portal.build_mask(scene(400, 500), 'oglasi')
        self.assertIsNone(mask)
        self.assertEqual(meta['reason'], 'unsupported_image_geometry')

    def test_double_stamp_uses_union_mask_and_preserves_every_outside_pixel(self):
        rgb = stamp(stamp(scene(), 'autodiler'), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi', include_other=True)
        self.assertTrue(meta['eligible'], meta)
        self.assertEqual({row['brand'] for row in meta['stamps']}, {'autodiler', 'oglasi'})
        result, _ = repair_rgb(rgb, mask, lambda pixels, binary: np.zeros_like(pixels))
        np.testing.assert_array_equal(result[mask == 0], rgb[mask == 0])
        self.assertTrue(np.all(result[mask > 0] == 0))

    def test_noop_inpainter_fails_residual_checks(self):
        rgb = stamp(scene(), 'autodiler')
        mask, meta = portal.build_mask(rgb, 'autodiler')
        encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: pixels, 'model', 'source',
                                        ocr=lambda pixels, box: {'brand_detected': False})
        self.assertIsNone(encoded)
        self.assertEqual(proof['qc'], 'failed')
        self.assertFalse(proof['residual_pre_encoding'][0]['passed'])

    def test_changed_mask_is_rejected_before_inference(self):
        rgb = stamp(scene(), 'autodiler')
        mask, meta = portal.build_mask(rgb, 'autodiler')
        mask[0, 0] = 255
        def forbidden_infer(*args):
            self.fail('A changed mask must never reach the model')
        with self.assertRaisesRegex(ValueError, 'mask checksum'):
            portal.repair(rgb, mask, meta, forbidden_infer, 'model', 'source')

    def test_successful_repair_records_encoded_residuals_dimensions_and_ceiling(self):
        rgb = stamp(scene(), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi')
        encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: np.full_like(pixels, 90),
                                      'model', 'source', ocr=lambda pixels, box: {'brand_detected': False})
        self.assertIsNotNone(encoded, proof)
        self.assertLessEqual(len(encoded), 400000)
        self.assertEqual(Image.open(io.BytesIO(encoded)).size, (rgb.shape[1], rgb.shape[0]))
        self.assertTrue(proof['outside_mask_unchanged_before_encoding'])
        self.assertTrue(all(row['passed'] for row in proof['residual_post_encoding']))

    def test_residual_in_delivered_encoding_is_rejected(self):
        rgb = stamp(scene(), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi')
        wrong = io.BytesIO()
        Image.fromarray(rgb).save(wrong, format='PNG')
        # Simulate an encoder accidentally returning the untouched original.
        with patch.object(portal, 'optimize', return_value=(wrong.getvalue(), (1200, 1600), (1200, 1600), 88)):
            encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: np.full_like(pixels, 90),
                                          'model', 'source', ocr=lambda pixels, box: {'brand_detected': False})
        self.assertIsNone(encoded)
        self.assertFalse(proof['residual_post_encoding'][0]['passed'])

    def test_resized_deliveries_check_the_scaled_stamp_and_record_dimensions(self):
        rgb = stamp(scene(), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi')
        def encoder(data, target, maximum, floor):
            im = Image.open(io.BytesIO(data)).resize((900, 1200), Image.Resampling.LANCZOS)
            out = io.BytesIO();im.save(out, format='WEBP', quality=80)
            return out.getvalue(), im.size, (1200, 1600), 80
        with patch.object(portal, 'optimize', side_effect=encoder):
            encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: np.full_like(pixels, 90),
                                          'model', 'source', ocr=lambda pixels, box: {'brand_detected': False})
        self.assertIsNotNone(encoded, proof)
        self.assertTrue(proof['resized_for_byte_limit'])
        self.assertTrue(proof['dimensions_preserved_before_encoding'])
        self.assertFalse(proof['dimensions_preserved'])
        self.assertEqual(proof['output_dimensions'], [900, 1200])

    def test_resized_original_with_remaining_stamp_is_rejected(self):
        rgb = stamp(scene(), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi')
        im = Image.fromarray(rgb).resize((900, 1200), Image.Resampling.LANCZOS)
        wrong = io.BytesIO();im.save(wrong, format='WEBP', quality=80)
        with patch.object(portal, 'optimize', return_value=(wrong.getvalue(), im.size, (1200, 1600), 80)):
            encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: np.full_like(pixels, 90),
                                          'model', 'source', ocr=lambda pixels, box: {'brand_detected': False})
        self.assertIsNone(encoded)
        self.assertFalse(proof['residual_post_encoding'][0]['passed'])

    def test_brand_ocr_residual_fails_even_after_word_shape_is_removed(self):
        rgb = stamp(scene(), 'oglasi')
        mask, meta = portal.build_mask(rgb, 'oglasi')
        encoded, proof = portal.repair(rgb, mask, meta, lambda pixels, binary: np.zeros_like(pixels),
                                      'model', 'source', ocr=lambda pixels, box: {'brand_detected': True})
        self.assertIsNone(encoded)
        self.assertEqual(proof['qc'], 'failed')


if __name__ == '__main__':
    unittest.main()
