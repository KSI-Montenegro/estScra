"""Persist photo inputs independently from successful listing exports; no image dependencies."""
import argparse
import csv
from datetime import datetime, timezone
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
    if not re.fullmatch(r'(estitor|patuljak|realitica|oglasi)_[0-9]+', key):
        raise ValueError('Invalid photo property key')
    urls = record.get('source_image_urls', record.get('image_urls', []))
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise ValueError('Image URL array required')
    return {'property_key': key, 'listing_url': record['listing_url'],
            'input_images': [{'image_number': n, 'original_url': u} for n, u in enumerate(urls, 1)],
            **{field: record[field] for field in ('first_seen_at', 'source_page_captured_at') if record.get(field)}}


def epoch(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.replace(tzinfo=timezone.utc).timestamp() if stamp.tzinfo is None else stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0


def ordered_pending(state, priority=()):
    """Explicit recovery targets, then freshest daily captures before historical work."""
    missing = set(priority) - state['properties'].keys()
    if missing:
        raise ValueError('Priority properties missing from photo queue: ' + ', '.join(sorted(missing)))
    positions = {key: n for n, key in enumerate(priority)}
    rank = {'daily': 0, 'backfill': 1, 'legacy': 2}
    return sorted((p for p in state['properties'].values() if p['phase'] == 'pending'),
                  key=lambda p: (positions.get(p['property_key'], len(positions)), rank.get(p.get('origin'), 3),
                                 -epoch(p.get('source_page_captured_at') or p.get('first_seen_at')),
                                 p['property_key']))


def enqueue(state, records, origin='daily'):
    for record in records:
        prop = input_property(record)
        signature = hashlib.sha256(json.dumps(prop['input_images'], sort_keys=True).encode()).hexdigest()
        old = state['properties'].get(prop['property_key'])
        if old and old.get('input_signature') == signature:
            for field in ('first_seen_at', 'source_page_captured_at'):
                if field in prop and (not old.get(field) or
                        (epoch(prop[field]) > epoch(old[field]) if field == 'source_page_captured_at'
                         else epoch(prop[field]) < epoch(old[field]))):
                    old[field] = prop[field]
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
    # Runners only retain this run's export directory. Recover recency for older
    # pending inputs from the durable cumulative records instead of FIFO order.
    contacts = state_path.with_name('contact-state.json')
    if contacts.exists():
        cumulative = json.loads(contacts.read_text())
        if cumulative.get('schema_version') != 1 or not isinstance(cumulative.get('records'), dict):
            raise ValueError('Contact state schema mismatch while preparing photos')
        records.update(cumulative['records'])
    for path in listings or sorted(Path(exports).glob('*/listings.json')):
        for record in json.loads(Path(path).read_text()):
            prior = records.get(record['property_key'])
            if prior is None or epoch(record.get('source_page_captured_at')) >= epoch(prior.get('source_page_captured_at')):
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
