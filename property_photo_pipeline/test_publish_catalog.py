import csv
import json
from pathlib import Path
import tempfile
import unittest

from build_triage import FIELDS
import publish_catalog as p


class Missing(Exception):
    response = {'Error': {'Code': '404'}}


class FakeS3:
    def __init__(self):
        self.objects = {}; self.puts = []
    def head_object(self, Bucket, Key):
        if Key not in self.objects: raise Missing()
        return self.objects[Key]
    def get_paginator(self, name): return self
    def paginate(self, **kwargs):
        return [{'Contents': [{'Size': r['ContentLength']} for r in self.objects.values()]}]
    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.objects[kwargs['Key']] = {'ContentLength': len(kwargs['Body']),
            'Metadata': kwargs['Metadata'], 'ETag': '"'+p.sha(kwargs['Body'])+'"'}


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.row = dict.fromkeys(FIELDS, '')
        self.row.update(property_key='estitor_123', listing_url='https://example.com/listing', image_number='1',
                        upload_status='uploaded', uploaded_url='https://cdn.example.com/properties/a.webp',
                        optimized_bytes='120', candidate_method='template', pattern_id='x', triage_group='known_logo_template_candidate',
                        active_url='https://cdn.example.com/properties/a.webp', repair_status='pending')
        self.write_rows()
        (self.root/'summary.json').write_text(json.dumps({'created_at':'2026-10-30T00:00:00Z','combined_candidate_images':1}))
        for name in ['watermark_candidates.csv','watermark_remaining.csv','cleaned_images.csv','per_property_triage.zip']:
            (self.root/name).write_bytes(b'test-artifact')
    def tearDown(self): self.tmp.cleanup()
    def write_rows(self, extra=False):
        with (self.root/'all_images.csv').open('w',newline='') as file:
            writer=csv.DictWriter(file,FIELDS+(['local_path'] if extra else []));writer.writeheader();writer.writerow(self.row)
    def test_pending_upload_and_private_fields_are_rejected(self):
        self.row.update(upload_status='planned',uploaded_url='');self.write_rows()
        with self.assertRaisesRegex(ValueError,'confirmed upload'):p.build_catalog(self.root)
        self.row.update(upload_status='uploaded',uploaded_url='https://cdn.example.com/properties/a.webp');self.write_rows(extra=True)
        with self.assertRaisesRegex(ValueError,'schema'):p.build_catalog(self.root)
    def test_publish_versions_and_conditional_owned_pointer(self):
        client=FakeS3()
        result=p.publish(client,'bucket','https://cdn.example.com',self.root,self.root/'output')
        self.assertEqual(result['available_images'],1)
        self.assertEqual(len(client.puts),8)
        self.assertTrue(all(call.get('IfNoneMatch')=='*' for call in client.puts))
        pointer=client.puts[-1]
        self.assertEqual(pointer['Key'],p.PREFIX+'/latest.json')
        p.publish(client,'bucket','https://cdn.example.com',self.root,self.root/'output')
        self.assertEqual(len(client.puts),9)
        self.assertIn('IfMatch',client.puts[-1])
    def test_repaired_url_requires_uploaded_verified_record(self):
        digest = 'a' * 64
        url = 'https://cdn.example.com/properties/estitor/123/cleaned/001-' + digest + '.webp'
        self.row.update(source_sha256='b'*64, active_url=url, cleaned_url=url, repair_status='processed',
                        repair_output_sha256=digest, repair_policy_id='patuljak-footer-stable-colors-v2-filled-word-bands',
                        repair_qc='mascot_and_ocr_only_pass_text_shape_unvalidated')
        self.write_rows()
        (self.root/'summary.json').write_text(json.dumps({'created_at':'2026-10-30T00:00:00Z','combined_candidate_images':1,'cleaned_count':1,'remaining_candidates':0}))
        item = p.build_catalog(self.root)['properties']['estitor_123']['images'][0]
        self.assertEqual(item['selected_version'], 'cleaned')
        self.assertEqual(item['active_url'], url)
        self.assertEqual(item['source_copy_url'], 'https://cdn.example.com/properties/a.webp')
        self.row['repair_qc'] = 'failed'
        self.write_rows()
        with self.assertRaisesRegex(ValueError, 'Invalid repaired'):
            p.build_catalog(self.root)
        self.row.update(repair_status='pending', repair_qc='', active_url=url)
        self.write_rows()
        with self.assertRaisesRegex(ValueError, 'cannot select'):
            p.build_catalog(self.root)

    def test_unowned_pointer_and_budget_refuse_all_writes(self):
        client=FakeS3()
        client.objects[p.PREFIX+'/latest.json']={'ContentLength':10,'Metadata':{'owner':'another-app'},'ETag':'"a"'}
        with self.assertRaisesRegex(ValueError,'not owned'):p.publish(client,'bucket','https://cdn.example.com',self.root,self.root/'output')
        self.assertFalse(client.puts)
        client.objects.clear()
        with self.assertRaisesRegex(ValueError,'budget'):p.publish(client,'bucket','https://cdn.example.com',self.root,self.root/'output',budget=0)
        self.assertFalse(client.puts)


if __name__ == '__main__': unittest.main()
