"""Publisher contract against a real Oglasi repair proof from the GitHub trial."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from photo_catalog import R2Store, repair_proof_matches


class OglasiCatalogTests(unittest.TestCase):
    def setUp(self):
        self.row=json.loads((Path(__file__).parent/'fixtures/oglasi_repair_proof.json').read_text())

    def test_live_oglasi_proof_is_accepted_by_catalog_publisher(self):
        self.assertTrue(repair_proof_matches(self.row))
        store=R2Store(None,'bucket',self.row['cleaned_url'].split('/properties/')[0])
        with patch.object(store,'verify') as verify:
            store.verify_image(self.row)
        verify.assert_called_once()

    def test_model_source_mask_and_encoded_residual_evidence_must_match(self):
        for change in ['source','model','mask','residual','bytes','brand','outside']:
            with self.subTest(change=change):
                row=deepcopy(self.row);proof=row['repair_qc']
                if change=='source':proof['source_sha256']='0'*64
                if change=='model':proof['model_sha256']='0'*64
                if change=='mask':proof['mask_sha256']='0'*64
                if change=='residual':proof['residual_post_encoding'][0].update(after=.3,passed=True)
                if change=='bytes':proof['output_bytes']+=1
                if change=='brand':proof['mask_metadata']['stamps'][0]['brand']='autodiler'
                if change=='outside':proof['outside_mask_unchanged_before_encoding']=False
                self.assertFalse(repair_proof_matches(row))

    def test_portal_policy_does_not_authorize_another_source_or_missing_evidence(self):
        row=deepcopy(self.row);row['cleaned_url']=row['cleaned_url'].replace('/oglasi/','/patuljak/')
        self.assertFalse(repair_proof_matches(row))
        row=deepcopy(self.row);row['repair_qc']='portal_word_shapes_and_focused_ocr_passed'
        self.assertFalse(repair_proof_matches(row))


if __name__=='__main__':unittest.main()
