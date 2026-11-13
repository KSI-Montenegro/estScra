"""Match normalized advertiser phones across property feeds without deleting ads."""
import argparse
from collections import defaultdict
from copy import deepcopy
import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
LEGACY = (HERE.parent if (HERE.parent/'collect_images.py').exists() else HERE) / 'listing_details/listings.json'


def load_records(path):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return list(csv.DictReader(stream))
    data = json.loads(path.read_text())
    if isinstance(data, dict) and isinstance(data.get('records'), dict):
        return list(data['records'].values())
    if isinstance(data, list) and all(isinstance(row, dict) for row in data):
        return data
    raise ValueError('Expected listing JSON array, contact-state.json, or CSV')


def phones(record):
    from contact_enrichment import normalize_phone, text_phones
    values = record.get('contact_phones') or []
    if isinstance(values, str):
        try:
            values = json.loads(values)
        except ValueError:
            values = text_phones(values.lstrip("'"))
    if not isinstance(values, list):
        values = []
    values = list(values)
    for field in ('phone_e164', 'phone', 'phone_numbers'):
        value = record.get(field)
        if isinstance(value, str):
            direct=normalize_phone(value.lstrip("'"),local=True)
            if direct:
                values.append(direct)
            values.extend(text_phones(value.lstrip("'")))
    return sorted({p for value in values if (p := normalize_phone(value, local=True))})


def identity(record):
    key = record.get('property_key')
    if key:
        return key
    if record.get('source') and record.get('listing_id'):
        return str(record['source']) + '_' + str(record['listing_id'])
    url = record.get('listing_url') or record.get('url') or record.get('ad_url')
    if url:
        import sys
        sys.path.insert(0, str(HERE.parent))
        from collect_images import identify
        try:
            source, ident = identify(url)
            return source + '_' + ident
        except ValueError:
            return url
    return None


def annotate(candidates, known):
    index = defaultdict(dict)
    for record in known:
        key = identity(record)
        if not key:
            continue
        ref = {'property_key': key, 'source': record.get('source'),
               'listing_url': record.get('listing_url') or record.get('url') or record.get('ad_url')}
        for phone in phones(record):
            index[phone][key] = ref
    result = []
    for record in candidates:
        row = deepcopy(record);key = identity(row);matches = [];refs = {}
        for phone in phones(row):
            other = [v for k,v in sorted(index[phone].items()) if k != key]
            if other:
                matches.append({'phone_e164': phone, 'listings': other})
                refs.update({v['property_key']:v for v in other})
        row.update(phone_match_status='existing_contact' if matches else 'new_contact' if phones(row) else 'no_phone',
                   phone_match_count=len(refs), matched_property_keys=sorted(refs), phone_matches=matches)
        result.append(row)
    return result


def legacy_records():
    if not LEGACY.exists():
        raise ValueError('Historical phone baseline missing: listing_details/listings.json')
    return load_records(LEGACY)


def report(records):
    return [{'property_key': identity(r), 'source': r.get('source'), 'listing_url': r.get('listing_url'),
             'contact_phones': phones(r), **{k:r[k] for k in (
                 'phone_match_status','phone_match_count','matched_property_keys','phone_matches')}} for r in records]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',type=Path,required=True)
    p.add_argument('--known',type=Path,nargs='+',default=[LEGACY, HERE/'runtime/contact-state.json'])
    p.add_argument('--output',type=Path,default=HERE/'contact_exports/phone_matches')
    args=p.parse_args()
    known=[r for path in args.known for r in load_records(path)]
    rows=report(annotate(load_records(args.candidates),known))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.with_suffix('.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2)+'\n')
    from daily_scraper import csv_text
    args.output.with_suffix('.csv').write_text(csv_text(list(rows[0]) if rows else [
        'property_key','phone_match_status','matched_property_keys'],rows))
    print(json.dumps({'checked':len(rows),'existing_contacts':sum(r['phone_match_status']=='existing_contact' for r in rows),
                      'new_contacts':sum(r['phone_match_status']=='new_contact' for r in rows),
                      'no_phone':sum(r['phone_match_status']=='no_phone' for r in rows),'output':str(args.output)}))


if __name__=='__main__':main()
