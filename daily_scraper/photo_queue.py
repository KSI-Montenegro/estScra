"""Persist photo inputs independently from successful listing exports; no image dependencies."""
import argparse
import csv
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def input_property(record):
    key = record['property_key']
    if not re.fullmatch(r'(estitor|patuljak|realitica)_[0-9]+', key):
        raise ValueError('Invalid photo property key')
    urls = record.get('image_urls', [])
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise ValueError('Image URL array required')
    return {'property_key': key, 'listing_url': record['listing_url'],
            'input_images': [{'image_number': n, 'original_url': u} for n, u in enumerate(urls, 1)]}


def enqueue(state, records, origin='daily'):
    for record in records:
        prop = input_property(record)
        signature = hashlib.sha256(json.dumps(prop['input_images'], sort_keys=True).encode()).hexdigest()
        old = state['properties'].get(prop['property_key'])
        if old and old.get('input_signature') == signature:
            if origin == 'daily' and old.get('phase') == 'pending':
                old['origin'] = 'daily'
            continue
        state['properties'][prop['property_key']] = {
            **prop, 'input_signature': signature, 'origin': origin, 'phase': 'pending',
            'images': (old or {}).get('images', {}), 'failures': (old or {}).get('failures', {}), 'sample': {}}


def legacy_inputs(manifest):
    groups = {}
    if not manifest or not Path(manifest).exists():
        return []
    with open(manifest, encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            prop = groups.setdefault(row['property_key'], {'property_key': row['property_key'], 'listing_url': row['listing_url'], 'numbered': {}})
            prop['numbered'][int(row['image_number'])] = row['image_url']
    return [{**{k: v for k, v in p.items() if k != 'numbered'},
             'image_urls': [p['numbered'][n] for n in sorted(p['numbered'])]} for p in groups.values()]


def prepare(state_path, exports, listings=(), legacy_manifest='property_images/all_image_urls.csv', backfill=None):
    state_path = Path(state_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else {'schema_version': 1, 'properties': {}}
    if state.get('schema_version') != 1:
        raise ValueError('Photo state schema mismatch')
    if not state.get('legacy_seeded'):
        enqueue(state, legacy_inputs(legacy_manifest), 'legacy')
        state['legacy_seeded'] = True
    backfill = Path(backfill) if backfill else Path(__file__).with_name('photo_backfill.json')
    if backfill.exists() and not state.get('backfill_seeded'):
        enqueue(state, json.loads(backfill.read_text()), 'backfill')
        state['backfill_seeded'] = True
    records = {}
    for path in listings or sorted(Path(exports).glob('*/listings.json')):
        for record in json.loads(Path(path).read_text()):
            records[record['property_key']] = record
    enqueue(state, list(records.values()))
    save(state_path, state)
    return state


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, default=Path('daily_scraper/runtime/photo-state.json'))
    p.add_argument('--exports', type=Path, default=Path('daily_scraper/exports'))
    args = p.parse_args()
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = prepare(args.state, args.exports)
    print(json.dumps({'photo_properties': len(state['properties']),
                      'pending_properties': sum(p['phase'] == 'pending' for p in state['properties'].values())}))


if __name__ == '__main__':
    main()
