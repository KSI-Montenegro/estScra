#!/usr/bin/env python3
"""Export public allowlisted repair results; never include local paths or raw errors."""
import argparse
import json
from pathlib import Path
from pipeline import write_json


def sanitize(record):
    fields = ('property_key', 'image_number', 'source_sha256', 'status', 'upload_status',
              'output_sha256', 'uploaded_url', 'qc', 'algorithm')
    result = {k: record.get(k, '') for k in fields}
    result['policy_id'] = record.get('mask_metadata', {}).get('policy_id', '')
    reasons = {'mask not eligible':'unreliable_watermark_location',
               'residual mascot or brand text detected':'residual_watermark_detected',
               'byte limit would require resizing original dimensions':'size_limit_at_original_resolution'}
    result['error'] = ('processing_failed' if record.get('status') == 'error' else
                       reasons.get(record.get('error'),'queued') if record.get('status') == 'queued' else '')
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', default='property_photo_pipeline/staging/local_removal/summary.json')
    p.add_argument('--output', default='property_photo_pipeline/reports/repair_results.json')
    args = p.parse_args()
    data = json.loads(Path(args.input).read_text())
    records = [sanitize(r) for r in data['records']]
    write_json(Path(args.output), {'version': 1, 'records': records})
    print(f'Exported {len(records)} sanitized repair records.')


if __name__ == '__main__':
    main()
