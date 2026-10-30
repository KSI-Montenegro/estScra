#!/usr/bin/env python3
"""Export property-grouped OCR triage, never treating a negative OCR as clean."""
import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import zipfile

from PIL import Image, ImageDraw, ImageOps
from pipeline import atomic_write, group, write_json

FIELDS = ['property_key', 'listing_url', 'image_number', 'image_url', 'source_sha256',
          'optimized_sha256', 'optimized_bytes', 'ocr_status', 'triage_group', 'pattern_id',
          'brands', 'positions', 'max_brand_text_confidence', 'brand_text', 'watermark_verified',
          'candidate_method', 'logo_template_score', 'logo_box_original_pixels',
          'auto_edit_approved', 'upload_status', 'object_key', 'expected_public_url', 'uploaded_url',
          'active_url', 'cleaned_url', 'repair_status', 'repair_output_sha256', 'repair_policy_id', 'repair_qc', 'repair_error']


def apply_repair(row, repair):
    """Never select a repaired copy without exact source, QC, upload and URL identity."""
    candidate = bool(row.get('pattern_id'))
    available = row.get('upload_status') in ('uploaded', 'existing') and bool(row.get('uploaded_url'))
    result = {'active_url': row['uploaded_url'] if available else '', 'cleaned_url': '',
              'repair_status': 'pending' if candidate else 'unverified_not_flagged',
              'repair_output_sha256': '', 'repair_policy_id': '', 'repair_qc': '', 'repair_error': ''}
    if not available:
        result['repair_status'] = 'unavailable'
    if not repair:
        return result
    if repair.get('source_sha256') != row.get('source_sha256'):
        result['repair_error'] = 'source_hash_mismatch'
        return result
    result['repair_policy_id'] = repair.get('policy_id', '')
    result['repair_qc'] = repair.get('qc', '')
    if repair.get('status') != 'processed' or repair.get('upload_status') not in ('uploaded', 'existing'):
        result['repair_error'] = repair.get('error', '') or 'not_uploaded_or_not_processed'
        return result
    digest = repair.get('output_sha256', '')
    url = repair.get('uploaded_url', '')
    try:
        source, ident = group(row['property_key'])
        number = int(row['image_number'])
        expected = f'/properties/{source}/{ident}/cleaned/{number:03d}-{digest}.webp'
        parsed = urlsplit(url)
        source_delivery = urlsplit(row['uploaded_url'])
        valid_url = (parsed.scheme == 'https' and parsed.netloc == source_delivery.netloc and not parsed.username
                     and parsed.path.endswith(expected) and not parsed.query and not parsed.fragment)
    except (ValueError, KeyError):
        valid_url = False
    valid = (candidate and available and re.fullmatch(r'[0-9a-f]{64}', row.get('source_sha256', '') or '')
             and re.fullmatch(r'[0-9a-f]{64}', digest or '') and valid_url
             and repair.get('qc') == 'mascot_and_ocr_only_pass_text_shape_unvalidated'
             and repair.get('algorithm') == 'local-lama-mask-v2-edge-pad'
             and repair.get('policy_id') == 'patuljak-footer-stable-colors-v2-filled-word-bands')
    if not valid:
        result['repair_error'] = 'repair_validation_failed'
        return result
    result.update(active_url=url, cleaned_url=url, repair_status='processed', repair_output_sha256=digest, repair_error='')
    return result


def write_csv(path, rows, fields):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore', lineterminator='\n')
    writer.writeheader(); writer.writerows(rows)
    atomic_write(path, buf.getvalue().encode())


def classify(row):
    hits = row.get('brand_hits', [])
    if row['status'] == 'download_error':
        return 'unavailable'
    if row.get('_logo', {}).get('match_status') == 'known_logo_template_candidate':
        return 'known_logo_template_candidate'
    if any(hit['brand'] == row['source'] and hit['position'].startswith('bottom_') for hit in hits):
        return 'footer_brand_text_candidate'
    if hits:
        return 'other_brand_text_candidate'
    return 'other_text_uncertain' if row['status'] == 'other_text_detected' else 'no_text_unverified' if row['status'] == 'no_text_detected' else 'ocr_error'


def pattern(row):
    logo_match = row.get('_logo', {}).get('match_status') == 'known_logo_template_candidate'
    if not row.get('brand_hits') and not logo_match:
        return ''
    ratio = row['width'] / row['height']
    aspect = 'portrait' if ratio < .85 else 'landscape' if ratio > 1.2 else 'square'
    if logo_match:
        return '__'.join([row['source'], 'patuljak_mascot', 'footer', aspect])
    brands = '+'.join(sorted({hit['brand'] for hit in row['brand_hits']}))
    positions = '+'.join(sorted({hit['position'] for hit in row['brand_hits']}))
    return '__'.join([row['source'], brands, positions, aspect])


def evidence(row):
    logo = row.get('_logo', {})
    if logo.get('match_status') == 'known_logo_template_candidate':
        return [{'box_original_pixels': json.loads(logo['bbox_xyxy']), 'ocr_confidence': float(logo['score'])*100}]
    return row['brand_hits']


def samples(rows, count=6):
    ordered = sorted(rows, key=lambda r: -max(h['ocr_confidence'] for h in evidence(r)))
    # Include different confidence levels and distinct properties, not six near duplicates.
    distinct = []; seen = set()
    for row in ordered:
        if row['property_key'] not in seen:
            distinct.append(row); seen.add(row['property_key'])
    if len(distinct) <= count:
        return distinct
    return [distinct[round(i*(len(distinct)-1)/(count-1))] for i in range(count)]


def contact_sheet(rows, cache, path, label):
    tile_w, tile_h, caption_h = 360, 270, 46
    canvas = Image.new('RGB', (tile_w*3, 44+(tile_h+caption_h)*((len(rows)+2)//3)), 'white')
    draw = ImageDraw.Draw(canvas)
    draw.text((10, 8), label[:130], fill='black')
    draw.text((10, 24), 'OCR candidate evidence. No removal approval or clean-image certification.', fill='black')
    for index, row in enumerate(rows):
        x, y = index % 3 * tile_w, 44 + index // 3 * (tile_h+caption_h)
        image_id = hashlib.sha256(row['image_url'].encode()).hexdigest()[:24]
        original = (cache / (image_id+'.image')).read_bytes()
        if hashlib.sha256(original).hexdigest() != row['source_sha256']:
            raise ValueError('Source hash mismatch while making contact sheet')
        with Image.open(io.BytesIO(original)) as source:
            thumb = ImageOps.exif_transpose(source).convert('RGB')
        width, height = thumb.size
        thumb.thumbnail((tile_w, tile_h))
        offset_x = x+(tile_w-thumb.width)//2; offset_y = y+(tile_h-thumb.height)//2
        canvas.paste(thumb, (offset_x, offset_y))
        for hit in evidence(row):
            a,b,c,d = hit['box_original_pixels']
            box = [offset_x+a*thumb.width/width, offset_y+b*thumb.height/height,
                   offset_x+c*thumb.width/width, offset_y+d*thumb.height/height]
            draw.rectangle(box, outline='red', width=2)
        draw.text((x+4,y+tile_h+4), f"{row['property_key']} / image {row['image_number']}", fill='black')
        logo = row.get('_logo', {})
        caption = 'Mascot template score '+logo['score'] if logo.get('match_status') == 'known_logo_template_candidate' else 'Brand text confidence '+str(max(h['ocr_confidence'] for h in row['brand_hits']))
        draw.text((x+4,y+tile_h+21), caption, fill='black')
    output = io.BytesIO(); canvas.save(output, 'JPEG', quality=85)
    atomic_write(path, output.getvalue())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ocr', default='watermark_detection/runs/ocr_full/results.json')
    parser.add_argument('--inventory', default='property_photo_pipeline/staging/r2_inventory/inventory.csv')
    parser.add_argument('--manifest', default='property_images/all_image_urls.csv')
    parser.add_argument('--logos', default='watermark_detection/runs/logo_match/results.csv')
    parser.add_argument('--cache', default='watermark_detection/cache')
    parser.add_argument('--repairs', default='property_photo_pipeline/reports/repair_results.json')
    parser.add_argument('--output', default='property_photo_pipeline/reports')
    args = parser.parse_args()
    raw = json.loads(Path(args.ocr).read_text())
    repairs = {}
    if Path(args.repairs).exists():
        for record in json.loads(Path(args.repairs).read_text())['records']:
            repair_key = (record['property_key'], str(int(record['image_number'])))
            if repair_key in repairs:
                raise ValueError('Duplicate repair record')
            repairs[repair_key] = record
    logos = {}
    if Path(args.logos).exists():
        with open(args.logos, newline='') as file:
            logos = {(r['property_key'], str(r['image_number'])): r for r in csv.DictReader(file)}
    with open(args.inventory, newline='') as file:
        inventory = {(r['property_key'], str(r['image_number'])): r for r in csv.DictReader(file)}
    with open(args.manifest, newline='') as file:
        listings = {(r['property_key'], str(r['image_number'])): r for r in csv.DictReader(file)}
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    rows = []; patterns = defaultdict(list); by_property = defaultdict(list)
    for original in raw:
        key = (original['property_key'], str(original['image_number']))
        target = inventory[key]; listing = listings[key]
        if original['image_url'] != target['image_url'] or original['image_url'] != listing['image_url']:
            raise ValueError('URL mismatch between OCR, input and upload inventory')
        if original.get('source_sha256', '') != target['source_sha256']:
            raise ValueError('Source hash mismatch between OCR and upload inventory')
        hits = original.get('brand_hits', [])
        logo = logos.get(key, {})
        if logo and logo.get('source_sha256', '') != original.get('source_sha256', ''):
            raise ValueError('Source hash mismatch between OCR and logo scan')
        original['_logo'] = logo
        has_logo = logo.get('match_status') == 'known_logo_template_candidate'
        row = {k: target.get(k, '') for k in FIELDS}
        row.update(property_key=key[0], image_number=key[1], image_url=original['image_url'],
                   listing_url=listing.get('listing_url', ''), ocr_status=original['status'],
                   triage_group=classify(original), pattern_id=pattern(original),
                   brands=';'.join(sorted({h['brand'] for h in hits})),
                   positions=';'.join(sorted({h['position'] for h in hits})),
                   max_brand_text_confidence=max((h['ocr_confidence'] for h in hits), default=''),
                   brand_text=' | '.join(dict.fromkeys(h['recognized_text'] for h in hits)),
                   candidate_method='template_and_ocr' if has_logo and hits else 'template' if has_logo else 'ocr' if hits else 'none',
                   logo_template_score=logo.get('score', ''), logo_box_original_pixels=logo.get('bbox_xyxy', ''),
                   watermark_verified=False, auto_edit_approved=False, object_key=target['key'])
        row.update(apply_repair(row, repairs.get((key[0], str(int(key[1]))))))
        rows.append(row); by_property[key[0]].append(row)
        if hits or has_logo:
            patterns[row['pattern_id']].append(original)
    assert len(rows) == len(inventory) == len(listings)
    assert all(not row['watermark_verified'] and not row['auto_edit_approved'] for row in rows)
    write_csv(output/'all_images.csv', rows, FIELDS)
    candidates = [r for r in rows if r['pattern_id']]
    write_csv(output/'watermark_candidates.csv', candidates, FIELDS)
    cleaned = [r for r in candidates if r['repair_status'] == 'processed']
    remaining = [r for r in candidates if r['repair_status'] != 'processed']
    write_csv(output/'watermark_remaining.csv', remaining, FIELDS)
    write_csv(output/'cleaned_images.csv', cleaned, FIELDS)
    write_csv(output/'unavailable_images.csv', [r for r in rows if r['triage_group']=='unavailable'], FIELDS)
    pattern_rows = []
    for name, items in sorted(patterns.items(), key=lambda item: -len(item[1])):
        selected = samples(items)
        contact_sheet(selected, Path(args.cache), output/'pattern_samples'/(name+'.jpg'), name)
        pattern_rows.append(dict(pattern_id=name, image_count=len(items), property_count=len({r['property_key'] for r in items}),
            sample_images=';'.join(r['property_key']+':'+str(r['image_number']) for r in selected),
            evidence_file='pattern_samples/'+name+'.jpg', rule_status='not_validated', edit_approved=False))
    write_csv(output/'pattern_groups.csv', pattern_rows, ['pattern_id','image_count','property_count','sample_images','evidence_file','rule_status','edit_approved'])
    # One portable artifact avoids thousands of extra per-property files in Git.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for key, items in sorted(by_property.items()):
            source, ident = group(key)
            file = io.StringIO(newline=''); writer = csv.DictWriter(file, FIELDS, lineterminator='\n'); writer.writeheader(); writer.writerows(items)
            archive.writestr(f'properties/{source}/{ident}/images.csv', file.getvalue())
    atomic_write(output/'per_property_triage.zip', buf.getvalue())
    summary = dict(created_at=datetime.now(timezone.utc).isoformat(), images=len(rows),
                   property_count=len(by_property), triage_groups=dict(Counter(r['triage_group'] for r in rows)),
                   known_brand_text_candidates=sum(bool(r.get('brand_hits')) for r in raw),
                   known_logo_template_candidates=sum(r.get('_logo', {}).get('match_status') == 'known_logo_template_candidate' for r in raw),
                   combined_candidate_images=len(candidates), cleaned_count=len(cleaned), remaining_candidates=len(remaining), logo_scan_rows=len(logos), pattern_groups=len(pattern_rows),
                   candidate_methods=dict(Counter(r['candidate_method'] for r in rows)),
                   upload_statuses=dict(Counter(r['upload_status'] for r in rows)),
                   verified_watermark_count=None, verified_clean_count=None,
                   automatic_edit_approvals=0, image_editing_calls=0,
                   interpretation='Candidate counts combine OCR and a known mascot template, not every possible watermark. No text or no template match does not certify clean. Expected URLs are not proof of upload.')
    write_json(output/'summary.json', summary)
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
