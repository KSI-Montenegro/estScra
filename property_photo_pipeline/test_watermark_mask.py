import json
import unittest
import numpy as np

from watermark_mask import build_mask


class WatermarkMaskTests(unittest.TestCase):
    def setUp(self):
        self.image = np.zeros((500,666,3),np.uint8)
        self.row = {'triage_group':'known_logo_template_candidate', 'logo_template_score':'1',
                    'logo_box_original_pixels':'[530,400,605,493]'}
    def test_known_graphic_mask_is_bounded_and_covers_complete_overlay(self):
        mask, meta = build_mask(self.row, self.image)
        self.assertTrue(meta['eligible'])
        self.assertEqual(meta['overlay_box_original_pixels'],[372,397,665,500])
        self.assertFalse(mask[:397].any())
        self.assertFalse(mask[:,:372].any())
        self.assertGreater(mask[440:500,376:535].sum(),0)
        self.assertGreater(mask[400:493,530:605].sum(),0)
        self.assertGreater(mask[447:490,605:665].sum(),0)
    def test_weaker_graphic_needs_independent_text_at_correct_pose(self):
        self.row.update(triage_group='footer_brand_text_candidate',logo_template_score='.7')
        hit={'brand':'patuljak','position':'bottom_right','ocr_confidence':90,'box_original_pixels':[379,444,534,488]}
        self.row['ocr_hits_json']=json.dumps([hit])
        self.assertTrue(build_mask(self.row,self.image)[1]['eligible'])
        hit['box_original_pixels']=[10,10,160,54]
        self.row['ocr_hits_json']=json.dumps([hit])
        mask,meta=build_mask(self.row,self.image)
        self.assertFalse(meta['eligible']);self.assertFalse(mask.any())
    def test_scene_text_or_weak_graphic_does_not_authorize_mask(self):
        self.row.update(triage_group='other_text_uncertain',logo_template_score='.7',ocr_hits_json='[]')
        self.assertFalse(build_mask(self.row,self.image)[1]['eligible'])
        self.row.update(triage_group='known_logo_template_candidate',logo_template_score='.4')
        self.assertFalse(build_mask(self.row,self.image)[1]['eligible'])


if __name__=='__main__':unittest.main()
