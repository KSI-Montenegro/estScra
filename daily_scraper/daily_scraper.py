#!/usr/bin/env python3
"""Discover property ads and export resumable daily feeds. Never logs credentials."""
import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import re
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from lxml import html

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import collect_images as galleries
import extract_listing_details as details

UA = 'Mozilla/5.0 (compatible; GradenDailyCollector/1.0)'
SOURCES = ('realitica', 'patuljak', 'estitor')
ALLOWED_HOSTS = set(galleries.HOSTS) | {'www.patuljak.me', 'www.estitor.com'}
MAX_BODY = 8_000_000


def now():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    galleries.atomic(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


class FetchFailure(Exception):
    """Only safe, fixed error codes may be exported."""


class Fetcher:
    def __init__(self, key, zone, mode='auto', max_api=100, interval=0.7):
        self.key, self.zone, self.mode = key, zone, mode
        self.max_api, self.interval = max_api, interval
        self.next_request = {}
        self.paused = set()
        self.api_blocked = False
        self.counts = Counter()

    def get(self, url, force_api=False):
        host = urlsplit(url).hostname
        if host not in ALLOWED_HOSTS or urlsplit(url).scheme != 'https':
            raise FetchFailure('unsupported_url')
        if host in self.paused:
            raise FetchFailure('source_rate_limited')
        time.sleep(max(0, self.next_request.get(host, 0) - time.monotonic()))
        self.next_request[host] = time.monotonic() + self.interval
        if self.mode != 'always' and not force_api:
            try:
                with urlopen(Request(url, headers={'User-Agent': UA}), timeout=30) as response:
                    body = response.read(MAX_BODY + 1)
                    final = response.geturl()
                if len(body) > MAX_BODY:
                    raise FetchFailure('response_too_large')
                if urlsplit(final).hostname not in ALLOWED_HOSTS:
                    raise FetchFailure('unexpected_redirect_host')
                self.counts['direct_requests'] += 1
                return body, final, 'direct_https'
            except HTTPError as exc:
                if exc.code == 429:
                    self.paused.add(host)
                    raise FetchFailure('source_http_429') from None
                if exc.code in (404, 410) or self.mode == 'direct':
                    raise FetchFailure('source_http_' + str(exc.code)) from None
            except (URLError, TimeoutError, OSError):
                if self.mode == 'direct':
                    raise FetchFailure('direct_connection_failed') from None
        if self.mode == 'direct':
            raise FetchFailure('direct_page_not_parseable')
        if not self.key:
            raise FetchFailure('brightdata_key_missing')
        if self.api_blocked:
            raise FetchFailure('brightdata_paused')
        if self.counts['brightdata_requests'] >= self.max_api:
            raise FetchFailure('brightdata_request_cap_reached')
        payload = {'zone': self.zone, 'url': url, 'format': 'json', 'method': 'GET',
                   'headers': {'User-Agent': UA}}
        req = Request('https://api.brightdata.com/request', data=json.dumps(payload).encode(),
                      headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
        self.counts['brightdata_requests'] += 1
        try:
            with urlopen(req, timeout=60) as response:
                envelope = json.loads(response.read(MAX_BODY * 2 + 1))
        except HTTPError as exc:
            if exc.code in (401, 402, 403, 429):
                self.api_blocked = True
            raise FetchFailure('brightdata_http_' + str(exc.code)) from None
        except (ValueError, URLError, TimeoutError, OSError):
            raise FetchFailure('brightdata_connection_or_response_failed') from None
        status = envelope.get('status_code')
        if status != 200:
            if status == 429:
                self.paused.add(host)
            raise FetchFailure('proxied_source_http_' + str(status))
        body = envelope.get('body')
        if not isinstance(body, str) or len(body.encode()) > MAX_BODY:
            raise FetchFailure('brightdata_body_invalid')
        # Bright Data may not expose the final redirect URL; canonical checks below
        # are mandatory before accepting a property page.
        return body.encode(), url, 'brightdata_https_api'


def listing_links(body, page_url, source):
    root = html.fromstring(body)
    found = {}
    for href in root.xpath('//a/@href'):
        url = urljoin(page_url, href).split('#')[0].split('?')[0]
        try:
            detected, ident = galleries.identify(url)
        except ValueError:
            continue
        if detected == source:
            found.setdefault(source + '_' + ident, url)
    return found


def searches(source):
    if source == 'realitica':
        return [('sale', 'Prodaja'), ('rent', 'DuziNajam'), ('holiday', 'Najam')]
    if source == 'estitor':
        return [('sale', 'namjena-prodaja'), ('rent', 'namjena-izdavanje')]
    return [('properties', '')]


def search_url(source, value, page):
    if source == 'realitica':
        return 'https://www.realitica.com/index.php?' + urlencode({
            'for': value, 'pState': 'Crna Gora', 'pZpa': 'Crna Gora',
            'qob': 'p-new', 'lng': 'hr', 'cur_page': page})
    if source == 'patuljak':
        return 'https://patuljak.me/c/nekretnine/namjena-sve/strana-' + str(page)
    suffix = '' if page == 0 else '/strana-' + str(page + 1)
    return 'https://estitor.com/me/nekretnine/' + value + suffix + '?sort=latest'


def discover(fetcher, sources, max_pages, known, overlap_pages):
    found, reports, errors = {}, [], []
    for source in sources:
        for label, value in searches(source):
            fingerprints, overlap, count = set(), 0, 0
            reason = 'page_cap_reached'
            for page in range(max_pages):
                url = search_url(source, value, page)
                try:
                    body, final, transport = fetcher.get(url)
                    links = listing_links(body, final, source)
                    if not links and transport == 'direct_https' and fetcher.mode != 'direct':
                        body, final, _ = fetcher.get(url, force_api=True)
                        links = listing_links(body, final, source)
                    if not links:
                        raise FetchFailure('no_listing_links_on_search_page')
                    digest = hashlib.sha256('\n'.join(sorted(links)).encode()).hexdigest()
                    if digest in fingerprints:
                        reason = 'repeated_page_requires_review'
                        break
                    fingerprints.add(digest)
                    found.update(links)
                    count += 1
                    overlap = overlap + 1 if all(k in known for k in links) else 0
                    if overlap_pages and overlap >= overlap_pages:
                        reason = 'known_overlap_reached'
                        break
                except (FetchFailure, ValueError) as exc:
                    errors.append({'source': source, 'stage': 'discovery', 'search': label,
                                   'page': page, 'code': str(exc) if isinstance(exc, FetchFailure) else 'html_parse_failed'})
                    reason = 'error'
                    break
            reports.append({'source': source, 'search': label, 'pages_scanned': count,
                            'stop_reason': reason, 'coverage': 'bounded_scan_not_full_site_census'})
    return found, reports, errors


def parse_listing(body, url, source, ident, captured_at):
    root = html.fromstring(body)
    final_canonical = root.xpath('//link[@rel="canonical"]/@href')
    if final_canonical:
        try:
            identity = galleries.identify(urljoin(url, final_canonical[0]))
        except ValueError:
            raise FetchFailure('canonical_not_a_listing') from None
        if identity != (source, ident):
            raise FetchFailure('canonical_listing_mismatch')
    selectors = {'estitor': '//h1', 'patuljak': '//h1', 'realitica': '//*[@id="listing_body"]'}
    if not root.xpath(selectors[source]):
        raise FetchFailure('listing_body_missing_or_challenge')
    images, _, method, _ = galleries.extract(body, url, source, ident)
    thumbnail_only = False
    if source == 'realitica' and not images:
        # Some ads expose only a property-specific preview, not full-size gallery
        # links. Keep the observed URL; never guess the hidden full-size path.
        for value in root.xpath('//img/@src | //img/@data-src'):
            observed = galleries.image_url(value, url)
            if observed and urlsplit(observed).hostname in ('realitica.com', 'www.realitica.com') and re.fullmatch(
                    r'/c_images/thumb_' + re.escape(ident) + r'_[^/]+\.(?:jpe?g|png|webp)', urlsplit(observed).path, re.I):
                if observed not in images:
                    images.append(observed)
        if images:
            thumbnail_only = True
            method = 'realitica_observed_listing_thumbnail_only'
    r = {'property_key': source + '_' + ident, 'source': source, 'listing_id': ident,
         'listing_url': url, 'field_sources': {}, 'source_attributes': {}, 'quality_flags': [],
         'price_on_request': False, 'source_page_captured_at': captured_at,
         'source_html_sha256': hashlib.sha256(body).hexdigest(), 'image_urls': images,
         'image_count': len(images), 'gallery_extraction_method': method}
    getattr(details, source)(root, r)
    if thumbnail_only:
        r['quality_flags'].append('only_low_resolution_preview_exposed')
    if not r.get('title'):
        raise FetchFailure('listing_title_missing')
    if not r.get('deal_type'):
        r['deal_type'] = details.deal(r['title'])
    if not r.get('property_type'):
        r['property_type'] = details.property_type(r['title'])
    # Only phone numbers explicitly exposed by this page; no calls, reveal POSTs,
    # account login, or copying contacts from unrelated listings.
    phones = sorted({p for href in root.xpath('//a[starts-with(@href,"tel:")]/@href')
                     if (p := details.phone(href[4:].split('?')[0]))})
    r['contact_phones'] = phones
    r['phone_e164'] = phones[0] if len(phones) == 1 else None
    if phones:
        r['field_sources']['contact_phones'] = 'page:tel_links'
    r['missing_core_fields'] = [k for k in ('title', 'description_original', 'city', 'deal_type', 'property_type') if not r.get(k)]
    if r.get('price_amount') is None and not r['price_on_request']:
        r['missing_core_fields'].append('price')
    if not images:
        r['missing_core_fields'].append('images')
    r['import_status'] = 'ready_for_mapping' if not r['missing_core_fields'] and not r['quality_flags'] else 'needs_review'
    r['watermark_status'] = 'sample_required' if images else 'no_images'
    r['watermark_sample_url'] = images[0] if images else None
    r['watermark_sample_image_number'] = 1 if images else None
    r['watermark_sample_result'] = 'not_checked'
    r['newness'] = 'previously_unseen_in_our_state_not_proof_of_new_publication'
    return r


def load_state(path, baseline):
    if path.exists():
        state = json.loads(path.read_text())
        if state.get('schema_version') != 1:
            raise ValueError('Unsupported state schema; refusing to discard deduplication state')
        return state
    return {'schema_version': 1, 'created_at': now(), 'last_run_at': None,
            'listings': json.loads(baseline.read_text())['listings']}


CSV_FIELDS = ['property_key', 'source', 'listing_id', 'listing_url', 'first_seen_at', 'source_page_captured_at',
              'newness', 'title', 'description_original', 'deal_type', 'property_type', 'price_amount',
              'price_currency', 'price_raw', 'price_on_request', 'city', 'district', 'street_address',
              'country', 'area_m2', 'lot_area_m2', 'bedrooms', 'bathrooms', 'published_date',
              'published_date_raw', 'updated_date', 'updated_date_raw', 'advertiser_name', 'phone_e164',
              'contact_phones', 'image_count', 'image_urls', 'import_status', 'missing_core_fields',
              'watermark_status', 'watermark_sample_url', 'watermark_sample_result']


def csv_text(fields, records):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fields, extrasaction='ignore')
    writer.writeheader()
    for record in records:
        row = {}
        for field in fields:
            value = record.get(field)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            # CSV spreadsheet formula injection; exact source strings remain in JSON.
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                value = "'" + value
            row[field] = value
        writer.writerow(row)
    return '\ufeff' + out.getvalue()


def export_run(folder, records, errors, summary):
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / 'listings.json', records)
    galleries.atomic(folder / 'listings.csv', csv_text(CSV_FIELDS, records))
    write_json(folder / 'errors.json', errors)
    write_json(folder / 'summary.json', summary)
    images = [dict(property_key=r['property_key'], listing_url=r['listing_url'], image_number=n, image_url=url)
              for r in records for n, url in enumerate(r['image_urls'], 1)]
    galleries.atomic(folder / 'image_urls.csv', csv_text(['property_key', 'listing_url', 'image_number', 'image_url'], images))
    samples = [{k: r.get(k) for k in ('property_key', 'listing_url', 'watermark_sample_url',
                                     'watermark_sample_image_number', 'watermark_sample_result')} for r in records if r['image_urls']]
    galleries.atomic(folder / 'watermark_samples.csv', csv_text(
        ['property_key', 'listing_url', 'watermark_sample_url', 'watermark_sample_image_number', 'watermark_sample_result'], samples))
    galleries.atomic(folder / 'README.txt',
        'JSON preserves exact source strings; CSV escapes spreadsheet formulas.\n'
        'These are newly captured listings, not necessarily newly published ads.\n'
        'image_urls.csv contains original source URLs, not newly uploaded R2 copies.\n'
        'watermark_samples.csv queues one image per new property; no visual analysis or editing is performed here.\n'
        'Existing cleaned image selections in the R2 catalog are unchanged.\n'
        'See summary.json for bounded discovery coverage, backlog, and errors.\n')
    archive = folder.parent / (folder.name + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.iterdir()):
            z.write(p, p.name)
    return archive


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sources', nargs='+', choices=SOURCES, default=list(SOURCES))
    p.add_argument('--mode', choices=('auto', 'always', 'direct'), default='auto', help='auto: direct HTTPS first, Bright Data fallback')
    p.add_argument('--zone', default=os.getenv('BRIGHTDATA_ZONE', 'esstitor_site_phone_scraper'))
    p.add_argument('--api-key-file', type=Path)
    p.add_argument('--max-api-requests', type=int, default=100)
    p.add_argument('--max-pages', type=int, default=10, help='Per search; bounded coverage is reported explicitly')
    p.add_argument('--overlap-pages', type=int, default=3, help='Stop after this many fully known pages; 0 disables early stop')
    p.add_argument('--max-listings', type=int, default=300, help='New captures per run; excess discoveries remain pending')
    p.add_argument('--interval', type=float, default=0.7)
    p.add_argument('--state', type=Path, default=HERE / 'runtime/state.json')
    p.add_argument('--baseline', type=Path, default=HERE / 'baseline.json')
    p.add_argument('--output', type=Path, default=HERE / 'exports')
    p.add_argument('--discover-only', action='store_true')
    args = p.parse_args(argv)
    if not (1 <= args.max_pages <= 2000 and 1 <= args.max_listings <= 100000 and
            0 <= args.max_api_requests <= 10000 and args.interval >= 0.5 and args.overlap_pages >= 0):
        p.error('Invalid limit; interval must be >= 0.5 seconds')
    args.state.parent.mkdir(parents=True, exist_ok=True)
    lock = (args.state.parent / 'scraper.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        p.error('Another scraper holds the state lock')
    key = os.getenv('BRIGHTDATA_API_KEY', '').strip()
    if args.api_key_file:
        key = args.api_key_file.read_text().strip()
    if args.mode == 'always' and not key:
        p.error('Set BRIGHTDATA_API_KEY or provide --api-key-file')
    state = load_state(args.state, args.baseline)
    fetcher = Fetcher(key, args.zone, args.mode, args.max_api_requests, args.interval)
    started = now()
    run_id = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S') + '-' + os.urandom(3).hex()
    run_folder = args.output / run_id
    records = []
    found, reports, errors = discover(fetcher, args.sources, args.max_pages, state['listings'], args.overlap_pages)
    for k, url in found.items():
        if k not in state['listings']:
            state['listings'][k] = {'url': url, 'first_seen_at': started, 'status': 'pending', 'attempts': 0}
        state['listings'][k]['last_seen_at'] = started
    write_json(args.state, state)
    # Resume a partially exported run, so a crash after a successful scrape cannot
    # silently remove the listing from all future daily feeds.
    for item in state['listings'].values():
        if item.get('status') == 'captured_unexported':
            record = item.get('pending_record')
            if not isinstance(record, dict):
                raise ValueError('Unexported capture missing its durable record')
            if record['source'] in args.sources:
                records.append(record)
    pending = [(k, v) for k, v in state['listings'].items()
               if k.split('_', 1)[0] in args.sources and v.get('status') in ('pending', 'retry')]
    # Round-robin sources, so a large Estitor backlog cannot starve Patuljak.
    queues = {s: [(k, v) for k, v in pending if k.startswith(s + '_')] for s in args.sources}
    ordered = []
    while any(queues.values()):
        for source in args.sources:
            if queues[source]:
                ordered.append(queues[source].pop(0))
    attempted = 0
    for k, item in ([] if args.discover_only else ordered[:max(0, args.max_listings - len(records))]):
        source, ident = k.split('_', 1)
        attempted += 1
        try:
            body, final, transport = fetcher.get(item['url'])
            try:
                if galleries.identify(final) != (source, ident):
                    raise FetchFailure('redirect_listing_mismatch')
            except ValueError:
                raise FetchFailure('redirect_not_a_listing') from None
            try:
                record = parse_listing(body, item['url'], source, ident, now())
            except FetchFailure as exc:
                if transport != 'direct_https' or fetcher.mode == 'direct' or str(exc).startswith('canonical_'):
                    raise
                body, _, transport = fetcher.get(item['url'], force_api=True)
                record = parse_listing(body, item['url'], source, ident, now())
            record.update(first_seen_at=item['first_seen_at'], transport=transport)
            records.append(record)
            item.update(status='captured_unexported', pending_record=record, captured_at=now())
        except Exception as exc:
            code = str(exc) if isinstance(exc, FetchFailure) else 'parse_or_transport_' + type(exc).__name__
            terminal = code in ('source_http_404', 'source_http_410', 'canonical_not_a_listing',
                                'canonical_listing_mismatch', 'redirect_not_a_listing', 'redirect_listing_mismatch')
            item.update(status='unavailable' if terminal else 'retry', last_error_code=code)
            errors.append({'source': source, 'stage': 'listing', 'property_key': k, 'code': code})
        item['attempts'] = item.get('attempts', 0) + 1
        write_json(args.state, state)
        if attempted % 20 == 0:
            print(json.dumps({'attempted': attempted, 'captured': len(records)}), flush=True)
    backlog = sum(v['status'] in ('pending', 'retry') for v in state['listings'].values())
    summary = {'schema_version': 1, 'run_id': run_id, 'started_at': started, 'finished_at': now(),
               'discovered_ids': len(found), 'captured_listings': len(records), 'attempted_listings': attempted,
               'by_source': dict(Counter(r['source'] for r in records)), 'pending_backlog': backlog,
               'errors': len(errors), 'requests': dict(fetcher.counts), 'discovery': reports,
               'coverage': 'bounded_scan; increase max-pages for deeper coverage',
               'photo_delivery': 'source_urls_only; optimization_and_R2_photo_upload_are_separate',
               'watermark_check': 'one_representative_image_queued_per_property; not_analyzed',
               'status': 'completed_with_errors' if errors else 'completed_with_backlog' if backlog else 'completed'}
    archive = export_run(run_folder, records, errors, summary)
    for record in records:
        item = state['listings'][record['property_key']]
        item.update(status='exported', exported_run_id=run_id)
        item.pop('pending_record', None)
    state['last_run_at'] = summary['finished_at']
    write_json(args.state, state)
    write_json(args.output / 'latest.json', {'run_id': run_id, 'zip': archive.name, 'summary': summary})
    print(json.dumps({'archive': str(archive), **summary}), flush=True)
    return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
