"""Regression cases for unsafe gallery sampling, residual holds, and agency routing."""
import copy
import unittest
from unittest.mock import patch
from watermark_policy import POLICY,agency_hits,analysis_record,attach_photos,decorate,listing_summary,ready
from photo_catalog import merge_catalog


def checked_image(status='clean',agencies=(),portals=()):
    return {'source_sha256':'source','active_url':'https://cdn.example/asset.webp','ready_for_listing':status=='clean',
            'watermark_analysis':{'policy':POLICY,'source_sha256':'source','status':status,
                                   'agency_brands':list(agencies),'portal_brands':list(portals)}}


class WatermarkPolicyTests(unittest.TestCase):
    def test_a_sample_pass_never_authorizes_unchecked_other_images(self):
        row={'ready_for_listing':True,'watermark_status':'sample_passed','active_url':'https://cdn.example/raw.webp'}
        self.assertFalse(ready(row))
        record={'property_key':'patuljak_1','image_urls':['source-a','source-b']}
        rows=[{**checked_image(),'image_number':1},{**row,'image_number':2}]
        out=attach_photos(record,{'images':rows})
        self.assertEqual(out['image_urls'],['https://cdn.example/asset.webp'])
        self.assertEqual(out['photo_audit_status'],'partial');self.assertEqual(out['photo_pending_count'],1)
        self.assertFalse(out['photos_ready_for_publication'])

    def test_old_cover_result_cannot_be_relabelled_as_a_new_per_image_check(self):
        from process_photos import requeue_per_image
        state={'properties':{'patuljak_1':{'phase':'complete','sample':{'result':'sample_passed','source_sha256':'old'},
                'images':{},'input_images':[{'image_number':1}]}}}
        requeue_per_image(state)
        prop=state['properties']['patuljak_1']
        self.assertEqual(prop['phase'],'pending');self.assertEqual(prop['sample'],{})
        prop['phase']='needs_review'
        requeue_per_image(state)
        self.assertEqual(prop['phase'],'needs_review')

    def test_analysis_is_bound_to_source_bytes(self):
        row=checked_image();row['source_sha256']='changed'
        self.assertFalse(ready(row))

    def test_a_cleaned_path_without_fresh_byte_bound_checks_is_not_ready(self):
        row={'selected_version':'cleaned','cleaned_url':'https://cdn.example/cleaned/old.webp','ready_for_listing':True}
        self.assertFalse(ready(row))
        row.update(checked_image());row.update(selected_version='cleaned',repair_output_sha256='expected',
            delivered_watermark_analysis={'policy':POLICY,'source_sha256':'other','status':'clean'})
        self.assertFalse(ready(row))

    def test_every_photo_requires_agency_evidence_before_all_agency_flag(self):
        photos=[checked_image('watermarked',['Kvadrat']),checked_image('watermarked',['Kvadrat'])]
        all_=listing_summary(photos,2)
        self.assertTrue(all_['all_photos_agency_branded']);self.assertEqual(all_['suggested_contact_queue'],'agents')
        self.assertFalse(listing_summary(photos,3)['all_photos_agency_branded'])
        self.assertFalse(listing_summary(photos+[checked_image()],3)['all_photos_agency_branded'])

    def test_portal_watermarks_do_not_imply_agency(self):
        row=checked_image('watermarked',portals=['patuljak'])
        summary=listing_summary([row],1)
        self.assertFalse(summary['all_photos_agency_branded'])
        self.assertEqual(summary['suggested_contact_queue'],'unclassified')

    def test_reported_agency_evidence_is_explicit_and_not_fabricated_per_image(self):
        summary=listing_summary([],8,'patuljak_72906')
        self.assertEqual(summary['suggested_contact_queue'],'agents')
        self.assertEqual(summary['reported_agency_brands'],['Kvadrat'])
        self.assertEqual(summary['agency_signal_status'],'operator_reported')
        self.assertFalse(summary['all_photos_agency_branded']);self.assertEqual(summary['agency_branded_photo_count'],0)

    def test_known_agency_names_require_recognized_words_and_confidence(self):
        words=[{'text':'Seaside','confidence':90,'line':'1','box_original_pixels':[10,10,80,30]},
               {'text':'Living','confidence':90,'line':'1','box_original_pixels':[90,10,150,30]}]
        hits=agency_hits(words,200,100)
        self.assertEqual(hits[0]['brand'],'Seaside Living')
        self.assertFalse(agency_hits([{**w,'confidence':20} for w in words],200,100))
        self.assertFalse(agency_hits([{**words[0],'text':'living room'}],200,100))

    def test_cleaned_file_with_a_residual_is_held_without_deleting_the_edit(self):
        row={**checked_image(),'selected_version':'cleaned','cleaned_url':'https://cdn.example/old.webp',
             'active_url':'https://cdn.example/old.webp','publication_hold':True,
             'delivered_watermark_analysis':{'status':'watermarked'}}
        self.assertFalse(ready(row));self.assertEqual(decorate(row)['watermark_status'],'watermarked')
        row['image_number']=1
        old={**row,'publication_hold':False,'ready_for_listing':True}
        merged=merge_catalog({'properties':{'patuljak_1':{'images':[old]}}},{'patuljak_1':{'images':[row]}})
        result=merged['properties']['patuljak_1']['images'][0]
        self.assertEqual(result['cleaned_url'],old['cleaned_url']);self.assertFalse(result['ready_for_listing'])

    def test_hold_survives_an_unrelated_catalog_update(self):
        row={'image_number':1,'selected_version':'cleaned','cleaned_url':'old','publication_hold':True,'ready_for_listing':False}
        merged=merge_catalog({'properties':{'patuljak_1':{'images':[row]}}},{})
        self.assertFalse(merged['properties']['patuljak_1']['images'][0]['ready_for_listing'])

    def test_patulkjak_original_is_not_cleared_by_negative_ocr_alone(self):
        from process_photos import Detector
        from test_photos import image_bytes
        detector=Detector()
        with patch('process_photos.match_image',return_value=(.1,[],1)),patch('process_photos.ocr',return_value=[]):
            self.assertEqual(detector.for_property(image_bytes(),'patuljak')['result'],'needs_review')
            self.assertEqual(detector.for_property(image_bytes(),'patuljak',delivered=True)['result'],'sample_passed')

    def test_watermark_names_stay_attached_to_original_after_cleanup(self):
        a=analysis_record({'result':'watermark_candidate','reason':'known_logo','score':.95,'hits':[]},'source')
        row={'source_sha256':'source','selected_version':'cleaned','cleaned_url':'clean','ready_for_listing':True,'watermark_analysis':a,'repair_output_sha256':'output',
             'delivered_watermark_analysis':{'policy':POLICY,'source_sha256':'output','status':'clean'}}
        decorate(row)
        self.assertEqual(row['watermark_portals'],['patuljak']);self.assertEqual(row['watermark_status'],'cleaned')


if __name__=='__main__':unittest.main()
