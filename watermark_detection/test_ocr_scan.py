import shutil
import unittest
import numpy as np
import cv2
import ocr_scan as o

class OCRTests(unittest.TestCase):
    def test_word_coordinates_are_mapped_to_original(self):
        tsv='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t20\t30\t100\t40\t85\tpatuljak\n'
        words=o.parse_tsv(tsv,2,500,400)
        self.assertEqual(words[0]['box_original_pixels'],[10,15,60,35])
        self.assertEqual(words[0]['confidence'],85)
    def test_brand_detection_is_not_edit_approval(self):
        words=[{'text':'patuljak.me','confidence':90,'line':'1','box_original_pixels':[400,400,600,450]}]
        hits=o.brand_hits(words,666,500)
        self.assertTrue(hits)
        self.assertTrue(all(not h['is_verified_watermark'] for h in hits))
        self.assertEqual(hits[0]['position'],'bottom_right')
    def test_scene_text_is_not_a_known_brand(self):
        words=[{'text':'KITCHEN','confidence':95,'line':'1','box_original_pixels':[10,10,200,50]}]
        self.assertEqual(o.brand_hits(words,666,500),[])
    def test_low_confidence_brand_not_accepted(self):
        words=[{'text':'patuljak','confidence':15,'line':'1','box_original_pixels':[400,400,600,450]}]
        self.assertEqual(o.brand_hits(words,666,500),[])
    @unittest.skipUnless(shutil.which('tesseract'),'Tesseract is required for the actual OCR integration check')
    def test_actual_tesseract_reads_controlled_brand(self):
        im=np.full((240,700,3),255,np.uint8)
        cv2.putText(im,'patuljak',(40,120),cv2.FONT_HERSHEY_SIMPLEX,2.5,(0,0,0),4,cv2.LINE_AA)
        words=o.ocr(im,shutil.which('tesseract'),30)
        self.assertTrue(o.brand_hits(words,700,240))

if __name__=='__main__':unittest.main()
