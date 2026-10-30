import unittest
from build_triage import apply_repair
from sanitize_repairs import sanitize


class RepairIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.row = {'property_key':'patuljak_123','image_number':'1','source_sha256':'a'*64,
                    'upload_status':'uploaded','uploaded_url':'https://cdn.example/properties/source.webp','pattern_id':'candidate'}
        self.url = 'https://cdn.example/properties/patuljak/123/cleaned/001-' + 'b'*64 + '.webp'
        self.record = {'property_key':'patuljak_123','image_number':'1','source_sha256':'a'*64,
                       'status':'processed','upload_status':'uploaded','output_sha256':'b'*64,
                       'uploaded_url':self.url,'qc':'mascot_and_ocr_only_pass_text_shape_unvalidated',
                       'policy_id':'patuljak-footer-stable-colors-v2-filled-word-bands','algorithm':'local-lama-mask-v2-edge-pad'}
    def test_only_exact_uploaded_repair_becomes_active(self):
        result = apply_repair(self.row, self.record)
        self.assertEqual(result['repair_status'], 'processed')
        self.assertEqual(result['active_url'], self.url)
        for change in ({'source_sha256':'c'*64}, {'upload_status':'not_uploaded'}, {'status':'queued'},
                       {'uploaded_url':'https://example.org/other'}, {'qc':'failed'}, {'algorithm':'old'}):
            record = dict(self.record, **change)
            result = apply_repair(self.row, record)
            self.assertEqual(result['active_url'], self.row['uploaded_url'])
            self.assertNotEqual(result['repair_status'], 'processed')
        row = dict(self.row, upload_status='unavailable', uploaded_url='')
        self.assertEqual(apply_repair(row, self.record)['active_url'], '')
    def test_sanitizer_never_exports_private_paths_and_raw_errors(self):
        record = dict(self.record, output_path='/private/ready', intermediate_path='/private/intermediate',
                      error='secret /private/path', status='error', mask_metadata={'policy_id': self.record['policy_id']})
        result = sanitize(record)
        self.assertNotIn('output_path', result)
        self.assertNotIn('intermediate_path', result)
        self.assertEqual(result['error'], 'processing_failed')
        self.assertNotIn('/private', repr(result))


if __name__ == '__main__':
    unittest.main()
