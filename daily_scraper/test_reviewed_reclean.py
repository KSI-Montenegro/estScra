"""Re-edit receipts must replace only the reviewed current version and survive stale workers."""
import copy
import json
from pathlib import Path
import unittest
import tempfile
from unittest.mock import patch
from photo_catalog import merge_catalog, reviewed_repair_matches, repair_proof_matches
from process_photos import requeue_per_image


class ReviewedRecleanTests(unittest.TestCase):
    def setUp(self):
        self.row=json.loads((Path(__file__).parent/'fixtures/patuljak_reviewed_reclean.json').read_text())
        self.key='patuljak_86717'
        self.old=copy.deepcopy(self.row)
        self.old.update(cleaned_url='https://cdn.example/old.webp',active_url='https://cdn.example/old.webp',
            repair_output_sha256=self.row['supersedes_output_sha256'],repair_policy_id='legacy',
            publication_hold=True,ready_for_listing=False)
        self.old.pop('supersedes_output_sha256',None)
        self.catalog={'properties':{self.key:{'images':[self.old]}}}

    def test_live_reviewed_proof_accepts_exact_source_output_and_review(self):
        self.assertTrue(reviewed_repair_matches(self.row))
        self.assertTrue(repair_proof_matches(self.row))
        for change in ('source','output','model','mask','review','residual','ocr','outside','size','delivered','portal'):
            with self.subTest(change=change):
                row=copy.deepcopy(self.row);proof=row['repair_qc']
                if change=='source':proof['source_sha256']='0'*64
                if change=='output':proof['output_sha256']='0'*64
                if change=='model':proof['model_sha256']='0'*64
                if change=='mask':proof['canonical_mask_sha256']='0'*64
                if change=='review':proof['visual_review']['output_sha256']='0'*64
                if change=='residual':proof['residual_logo_score']=.9
                if change=='ocr':proof['focused_ocr']['brand_detected']=True
                if change=='outside':proof['outside_mask_unchanged_before_encoding']=False
                if change=='size':proof['dimensions_preserved_before_encoding']=False
                if change=='delivered':row['delivered_watermark_analysis']['status']='watermarked'
                if change=='portal':row['cleaned_url']=row['cleaned_url'].replace('/patuljak/','/estitor/')
                self.assertFalse(reviewed_repair_matches(row))

    def test_reviewed_replacement_preserves_old_object_history(self):
        result=merge_catalog(self.catalog,{self.key:{'images':[self.row]}})['properties'][self.key]['images'][0]
        self.assertEqual(result['active_url'],self.row['cleaned_url'])
        self.assertTrue(result['ready_for_listing'])
        self.assertTrue(result['previous_cleaned_versions'][0]['publication_hold'])
        self.assertEqual(result['previous_cleaned_versions'][0]['output_sha256'],self.old['repair_output_sha256'])

    def test_stale_or_invalid_replacement_cannot_promote(self):
        for change in ('supersedes','source','proof'):
            row=copy.deepcopy(self.row)
            if change=='supersedes':row['supersedes_output_sha256']='0'*64
            if change=='source':row['source_sha256']='0'*64
            if change=='proof':row['repair_qc']['visual_review']['decision']='pending'
            with self.subTest(change=change),self.assertRaises(ValueError):
                merge_catalog(self.catalog,{self.key:{'images':[row]}})

    def test_restored_old_worker_cannot_overwrite_new_version_evidence(self):
        current=merge_catalog(self.catalog,{self.key:{'images':[self.row]}})
        result=merge_catalog(current,{self.key:{'images':[self.old]}})['properties'][self.key]['images'][0]
        self.assertEqual(result['active_url'],self.row['cleaned_url'])
        self.assertTrue(result['ready_for_listing'])
        self.assertTrue(reviewed_repair_matches(result))

    def test_incident_migration_keeps_only_byte_bound_reviewed_release(self):
        legacy=copy.deepcopy(self.old)
        state={'properties':{self.key:{'phase':'needs_review','images':{'3':copy.deepcopy(self.row),'4':legacy},'input_images':[]}}}
        requeue_per_image(state)
        self.assertFalse(state['properties'][self.key]['images']['3']['publication_hold'])
        self.assertTrue(state['properties'][self.key]['images']['4']['publication_hold'])

    def test_rerun_keeps_exact_visual_review_for_scene_text_but_revokes_a_brand_hit(self):
        from test_photos import FakeDownloader, FakeStore, image_bytes
        from photo_queue import enqueue
        from photo_catalog import sha
        from process_photos import PhotoWorker
        for positive in (False,True):
            with self.subTest(positive_brand=positive),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);original=image_bytes();delivered=image_bytes('red')
                row=copy.deepcopy(self.row);row.update(image_number=1,source_sha256=sha(original),
                    repair_input_sha256=sha(original),repair_output_sha256=sha(delivered),upload_confirmed=False)
                row['watermark_analysis']['source_sha256']=sha(original)
                row['delivered_watermark_analysis']['source_sha256']=sha(delivered)
                row['repair_qc'].update(source_sha256=sha(original),output_sha256=sha(delivered))
                row['repair_qc']['visual_review']['output_sha256']=sha(delivered)
                self.assertTrue(reviewed_repair_matches(row))
                url='https://patuljak.me/original.webp';row['original_url']=url
                catalog={'properties':{self.key:{'images':[row]}}}
                state={'schema_version':1,'properties':{}}
                enqueue(state,[{'property_key':self.key,'listing_url':'https://patuljak.me/oglas/a--86717','image_urls':[url]}])
                prop=state['properties'][self.key]
                worker=PhotoWorker(state,root/'state.json',root/'out',FakeStore(catalog),None,
                    FakeDownloader(root/'cache',{url:original,row['cleaned_url']:delivered}),root/'unused.pt')
                scan={'result':'watermark_candidate' if positive else 'needs_review',
                      'reason':'known_logo_or_brand' if positive else 'unclassified_text','score':.3,
                      'hits':[{'brand':'patuljak'}] if positive else []}
                # Force the existing delivered bytes through a new audit instead
                # of taking the normal already-checked checkpoint fast path.
                with patch('process_photos.checked',return_value=False),patch.object(worker,'analyze_property',return_value=scan):
                    worker.process(prop)
                result=prop['images']['1']
                self.assertEqual(result['ready_for_listing'],not positive)
                self.assertEqual(result['publication_hold'],positive)


if __name__=='__main__':unittest.main()
