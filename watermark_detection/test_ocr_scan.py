import shutil
import unittest
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch
import numpy as np
import cv2
import ocr_scan as o

class OCRTests(unittest.TestCase):
    def test_ocr_cache_is_invalidated_when_original_bytes_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache=Path(tmp);raw=cache/'example.image';raw.write_bytes(b'original-v1')
            class Downloader:
                def __init__(self):self.cache=cache
                def get(self,row):return np.zeros((10,20,3),np.uint8),'cached'
            row={'image_id':'example','property_key':'patuljak_123','image_number':'1','image_url':'https://example.com/a.jpg'}
            with patch.object(o,'ocr',return_value=[]) as recognize:
                first=o.scan_cached_image(row,Downloader(),cache,'unused',30,'test',o.BRANDS,'test')
                reused=o.scan_cached_image(row,Downloader(),cache,'unused',30,'test',o.BRANDS,'test')
                raw.write_bytes(b'original-v2')
                changed=o.scan_cached_image(row,Downloader(),cache,'unused',30,'test',o.BRANDS,'test')
            self.assertEqual(recognize.call_count,2)
            self.assertFalse(first['ocr_cache_reused']);self.assertTrue(reused['ocr_cache_reused'])
            self.assertFalse(changed['ocr_cache_reused'])
            self.assertEqual(changed['source_sha256'],hashlib.sha256(b'original-v2').hexdigest())
            self.assertFalse(changed['edit_approved'])

    def test_disk_reserve_stops_new_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            class Downloader:
                cache=Path(tmp)
                def get(self,row):raise AssertionError('Network must not be called')
            result=o.scan_cached_image({'image_id':'missing'},Downloader(),Path(tmp),'unused',30,'test',o.BRANDS,'test',10**30)
            self.assertEqual(result['error'],'disk_reserve_reached')
            self.assertFalse(result['edit_approved'])
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
