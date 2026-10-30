#!/usr/bin/env python3
"""Join exact-source OCR evidence to the local removal candidate queue."""
import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from pipeline import write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates', default='property_photo_pipeline/reports/watermark_candidates.csv')
    parser.add_argument('--ocr', default='watermark_detection/runs/ocr_full/results.json')
    parser.add_argument('--output', default='property_photo_pipeline/staging/local_removal_inputs')
    args = parser.parse_args()
    original = json.loads(Path(args.ocr).read_text())
    lookup = {(r['property_key'],str(r['image_number'])):r for r in original}
    with open(args.candidates,newline='',encoding='utf-8-sig') as file:
        reader = csv.DictReader(file); fields = list(reader.fieldnames); candidates = list(reader)
    if 'ocr_hits_json' not in fields: fields.append('ocr_hits_json')
    for row in candidates:
        source = lookup[(row['property_key'],str(row['image_number']))]
        if source.get('source_sha256') != row['source_sha256'] or source['image_url'] != row['image_url']:
            raise ValueError('Candidate and OCR captured source do not agree')
        row['ocr_hits_json'] = json.dumps(source.get('brand_hits',[]),separators=(',',':'))
    output = Path(args.output); output.mkdir(parents=True,exist_ok=True)
    with (output/'candidates.csv').open('w',newline='',encoding='utf-8') as file:
        writer = csv.DictWriter(file,fields,lineterminator='\n');writer.writeheader();writer.writerows(candidates)
    summary = {'candidates':len(candidates),'candidate_methods':dict(Counter(r['candidate_method'] for r in candidates)),
               'interpretation':'Evidence enrichment only; strict mask policy and repair QC still determine processing eligibility.'}
    write_json(output/'summary.json',summary)
    print(json.dumps(summary))


if __name__ == '__main__': main()
