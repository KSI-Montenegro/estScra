"""Resumable, listing-scoped contact collection and versioned R2 listing delivery."""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urljoin
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
import zipfile

from lxml import html
from url_utils import transport_url

HERE = Path(__file__).resolve().parent
PLATFORM_PHONE = '+38269257004'
REVEAL_URL = 'https://api-v2.estitor.com/api/v1/calls/reveals'
OWNER = 'graden-listing-contacts'
PREFIX = 'graden-imports/listings'
TERMINAL = {'found', 'multiple_numbers', 'login_required', 'no_public_number', 'unavailable'}


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    from photo_queue import save as atomic_save
    atomic_save(path, data)


def normalize_phone(value, local=False):
    if not isinstance(value, str):
        return None
    value = re.sub(r'[\s()./\-]', '', value)
    if value.startswith('00'):
        value = '+' + value[2:]
    if local and re.fullmatch(r'0(?:20|30|31|32|33|40|41|50|51|52|60|63|67|68|69)\d{6}', value):
        value = '+382' + value[1:]
    if local and re.fullmatch(r'382[2-6]\d{7}', value):
        value = '+' + value
    if value.startswith('+382') and not re.fullmatch(r'\+382[2-6]\d{7}', value):
        return None
    if re.fullmatch(r'\+[1-9]\d{6,14}', value) and value != PLATFORM_PHONE:
        return value
    return None


def text_phones(text):
    # Only the extracted description of this ad, never whole-page text, adverts or footers.
    pattern = (r'(?<![\w+])(?:(?:\+|00)382(?:[ ()./\-]?\d){8}(?!\d)'
               r'|(?:\+|00)(?!382)[1-9](?:[ ()./\-]?\d){6,14}(?!\d)'
               r'|0(?:20|30|31|32|33|40|41|50|51|52|60|63|67|68|69)(?:[ ()./\-]?\d){6}(?!\d))')
    return sorted({p for m in re.finditer(pattern, text or '')
                   if (p := normalize_phone(m.group(), local=True))})


def page_contacts(root, record):
    candidates = []
    def add(value, origin, local=False):
        p = normalize_phone(value, local)
        if p and not any(c['phone_e164'] == p and c['source'] == origin for c in candidates):
            candidates.append({'phone_e164': p, 'source': origin, 'listing_url': record['listing_url']})
    for p in record.get('contact_phones', []):
        add(p, 'existing_contact')
    if record.get('phone_e164'):
        add(record['phone_e164'], 'existing_contact')
    for p in text_phones(record.get('description_original', '')):
        add(p, 'page:listing_description')
    scope = root
    if record['source'] == 'patuljak':
        nodes = root.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," product_full ")]')
        if nodes:
            scope = nodes[0]
    if record['source'] == 'oglasi':
        from oglasi_properties import current_ad
        if root.xpath('//script[@id="__NEXT_DATA__"]'):
            ad=current_ad(root, str(record['listing_id']))
            for item in (ad.get('user') or {}).get('phones', []):
                if isinstance(item, dict):
                    add(item.get('phone'), 'page:currentAd.user.phones', local=True)
        # Do not collect navigation, recommendation, or profile-directory numbers.
        return candidates
    for a in scope.xpath('.//a[starts-with(@href,"tel:")]'):
        if any(e.tag in ('footer', 'aside') or any(v in (e.get('class', '') + ' ' + e.get('id', '')).lower()
               for v in ('related', 'newsletter', 'popup', 'suggestion')) for e in a.iterancestors()):
            continue
        add(a.get('href')[4:].split('?')[0], 'page:tel_links', local=True)
    if record['source'] == 'realitica':
        # Read the existing phone-button handler; never execute JS or send a message.
        buttons = root.xpath('//*[@id="aboutAuthor"]//button')
        for node in buttons:
            button = node.get('id', '')
            if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', button) or not re.search(r'telef|phone|broj', node.text_content(), re.I):
                continue
            pattern = (r'\$\([\'"]#' + re.escape(button) + r'[\'"]\)\.click\(function\(\)\s*\{(.*?)\}\);')
            for script in root.xpath('//script[not(@src)]/text()'):
                match = re.search(pattern, script, re.S)
                if match:
                    number = re.search(r'\bvar\s+p\s*=\s*"((?:\\.|[^"\\]){1,1000})"', match.group(1))
                    if number:
                        try:
                            value = json.loads('"' + number.group(1) + '"')
                        except ValueError:
                            continue
                        for p in text_phones(value):
                            add(p, 'page:realitica_phone_button')
                        compact = re.sub(r'[\s()./\-]', '', value)
                        if re.fullmatch(r'\d{7,15}', compact):
                            add('+' + compact, 'page:realitica_phone_button')
    return candidates


def apply_result(record, result):
    row = deepcopy(record)
    existing = list(row.get('contact_candidates', []))
    for c in result.get('candidates', []):
        if c not in existing:
            existing.append(c)
    phones = set(row.get('contact_phones', []))
    if row.get('phone_e164'):
        phones.add(row['phone_e164'])
    phones.update(c['phone_e164'] for c in result.get('candidates', []))
    phones = sorted({p for v in phones if (p := normalize_phone(v))})
    row.update(contact_phones=phones, phone_e164=phones[0] if len(phones) == 1 else None,
               contact_candidates=existing, phone_collection_status=result.get('status', 'pending'),
               phone_checked_at=result.get('checked_at'), phone_error_code=result.get('error_code'))
    if phones:
        row.setdefault('field_sources', {})['contact_phones'] = 'contact_candidates:see_source_and_listing_url'
        row['field_sources']['phone_e164'] = 'single_unambiguous_contact' if len(phones) == 1 else 'multiple_numbers_review'
        row['missing_core_fields'] = [k for k in row.get('missing_core_fields', []) if k != 'contact_phone']
    if len(phones) > 1:
        flags = row.setdefault('quality_flags', [])
        if 'multiple_contact_phones_review' not in flags:
            flags.append('multiple_contact_phones_review')
    # Collection never asserts private-owner identity or outreach readiness.
    return row


class ContactFailure(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def quota_response(status, body):
    return status == 429 or bool(re.search(r'QUOTA|LIMIT|TOO[_ ]MANY', str(body), re.I))


class RevealClient:
    def __init__(self, key='', zone='esstitor_site_phone_scraper', max_api=20):
        self.key, self.zone, self.max_api = key, zone, max_api
        self.api_requests = 0

    def reveal(self, record):
        payload = {'targetType': 'AD', 'adId': int(record['listing_id']), 'contactSource': 'DETAILS_PAGE',
                   'language': 'me', 'pageUrl': record['listing_url']}
        headers = {'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json', 'Content-Type': 'application/json',
                   'Origin': 'https://estitor.com', 'Referer': transport_url(record['listing_url'])}
        req = Request(REVEAL_URL, data=json.dumps(payload).encode(), headers=headers)
        try:
            with urlopen(req, timeout=25) as response:
                data = json.loads(response.read(100_000))
        except HTTPError as exc:
            body = exc.read(4000).decode(errors='replace')
            if quota_response(exc.code, body):
                wait = 3600 if 'HOURLY' in body.upper() and 'DAILY' not in body.upper() else 86400
                value = exc.headers.get('Retry-After', '')
                if value.isdigit():
                    wait = max(wait, int(value))
                raise ContactFailure('estitor_quota_or_rate_limit', time.time() + wait) from None
            if exc.code == 404:
                raise ContactFailure('reveal_unavailable') from None
            if exc.code != 403 or not self.key:
                raise ContactFailure('reveal_http_' + str(exc.code)) from None
            # JSON 403s are application/auth responses, not evidence of a network block.
            if not re.search(r'<html|cloudflare|cloudfront|access denied|request blocked', body, re.I):
                raise ContactFailure('reveal_http_403') from None
            # Fallback only after a definite block, never after an ambiguous POST timeout.
            if self.api_requests >= self.max_api:
                raise ContactFailure('contact_api_request_cap') from None
            self.api_requests += 1
            envelope = {'zone': self.zone, 'url': REVEAL_URL, 'format': 'json', 'method': 'POST',
                        'headers': headers, 'body': json.dumps(payload)}
            provider = Request('https://api.brightdata.com/request', data=json.dumps(envelope).encode(),
                               headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'})
            try:
                with urlopen(provider, timeout=60) as response:
                    reply = json.loads(response.read(200_000))
            except HTTPError as error:
                raise ContactFailure('contact_provider_http_' + str(error.code)) from None
            except (ValueError, URLError, TimeoutError, OSError):
                raise ContactFailure('contact_provider_transport_unknown') from None
            status, body = reply.get('status_code'), reply.get('body', '')
            if quota_response(status, body):
                wait = 3600 if 'HOURLY' in str(body).upper() and 'DAILY' not in str(body).upper() else 86400
                raise ContactFailure('estitor_quota_or_rate_limit', time.time() + wait)
            if status != 200:
                raise ContactFailure('reveal_proxied_http_' + str(status))
            try:
                data = json.loads(body) if isinstance(body, str) else body
            except ValueError:
                raise ContactFailure('reveal_invalid_json') from None
        except (ValueError, URLError, TimeoutError, OSError):
            raise ContactFailure('reveal_transport_unknown') from None
        if not isinstance(data, dict) or 'phone' not in data:
            raise ContactFailure('reveal_invalid_schema')
        return data


def result_from_candidates(candidates, fallback='no_public_number'):
    count = len({c['phone_e164'] for c in candidates})
    return {'status': 'found' if count == 1 else 'multiple_numbers' if count else fallback,
            'candidates': candidates, 'checked_at': now()}


def load_inputs(state_path, exports, backfill):
    state = json.loads(Path(state_path).read_text()) if Path(state_path).exists() else {
        'schema_version': 1, 'records': {}, 'results': {}, 'estitor_gate': {}}
    if state.get('schema_version') != 1:
        raise ValueError('Unsupported contact checkpoint schema')
    paths = ([Path(backfill)] if backfill and Path(backfill).exists() else []) + sorted(Path(exports).glob('*/listings.json'))
    for path in paths:
        for row in json.loads(path.read_text()):
            key = row['property_key']
            if not re.fullmatch(r'(realitica|patuljak|estitor|oglasi)_\d+', key):
                raise ValueError('Invalid contact property key')
            if key != row['source'] + '_' + str(row['listing_id']):
                raise ValueError('Contact property identity mismatch')
            from daily_scraper import galleries
            if galleries.identify(row['listing_url']) != (row['source'], str(row['listing_id'])):
                raise ValueError('Contact URL identity mismatch')
            old = state['records'].get(key)
            if old is None or (row.get('source_page_captured_at') or '') >= (old.get('source_page_captured_at') or ''):
                state['records'][key] = apply_result(row, state['results'].get(key, {}))
    return state


def collect(state, state_path, fetcher, reveal, cache, max_listings=300, max_reveals=10, max_seconds=600, cookie=''):
    deadline = time.monotonic() + max_seconds
    counts = Counter()
    gate = state.setdefault('estitor_gate', {})
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    api_cap = fetcher.max_api
    rows = sorted(state['records'].items(), key=lambda kv: kv[1].get('first_seen_at', ''), reverse=True)
    for key, row in rows:
        previous = state['results'].get(key, {})
        if previous.get('status') in TERMINAL and not (cookie and previous.get('status') == 'login_required'):
            continue
        if counts['listings_checked'] >= max_listings or time.monotonic() >= deadline:
            counts['runtime_or_listing_limit'] += 1
            break
        legacy_auth_error = cookie and row['source'] == 'patuljak' and previous.get('error_code') == 'contact_HTTPError'
        if previous.get('retry_after', 0) > time.time() and not legacy_auth_error:
            continue
        candidates = page_contacts(html.fromstring('<html/>'), row)
        if row['source'] == 'estitor' and not candidates and (
                gate.get('resume_not_before', 0) > time.time() or counts['reveal_requests'] >= max_reveals):
            status = 'quota_deferred' if gate.get('resume_not_before', 0) > time.time() else 'pending'
            result = {**previous, 'status': status, 'candidates': []}
            state['results'][key] = result
            state['records'][key] = apply_result(row, result)
            counts['quota_or_reveal_limit_deferred'] += 1
            continue
        counts['listings_checked'] += 1
        result = result_from_candidates(candidates)
        if not candidates:
            try:
                if row['source'] == 'estitor':
                    if gate.get('resume_not_before', 0) > time.time() or counts['reveal_requests'] >= max_reveals:
                        result = {'status': 'quota_deferred' if gate.get('resume_not_before', 0) > time.time() else 'pending',
                                  'candidates': [], 'checked_at': now()}
                    else:
                        time.sleep(max(0, gate.get('last_request_at', 0) + 3.1 - time.time()))
                        counts['reveal_requests'] += 1
                        gate['last_request_at'] = time.time()
                        state['results'][key] = {'status': 'retry', 'candidates': [], 'checked_at': now(),
                                                 'error_code': 'reveal_inflight_or_interrupted',
                                                 'retry_after': time.time() + 3600}
                        save(state_path, state)
                        reveal.max_api = max(0, api_cap - fetcher.counts['brightdata_requests'] -
                                             fetcher.counts['brightdata_browser_requests'])
                        data = reveal.reveal(row)
                        p = normalize_phone(data.get('phone'), local=True)
                        candidates = ([{'phone_e164': p, 'source': 'estitor:listing_phone_reveal',
                                        'listing_url': row['listing_url']}] if p else [])
                        result = result_from_candidates(candidates)
                        gate['remaining_hour'], gate['remaining_day'] = data.get('remainingHour'), data.get('remainingDay')
                        if str(data.get('remainingHour')) == '0':
                            gate['resume_not_before'] = time.time() + 3600
                        if str(data.get('remainingDay')) == '0':
                            gate['resume_not_before'] = time.time() + 86400
                        # Conservative cooldowns, not a claim about the exact server reset time.
                else:
                    path = cache / (key + ('.authenticated' if cookie and row['source'] == 'patuljak' else '') + '.html')
                    if path.exists():
                        body = path.read_bytes()
                    else:
                        if row['source'] == 'patuljak' and cookie:
                            host = urlsplit(row['listing_url']).hostname
                            time.sleep(max(0, fetcher.next_request.get(host, 0) - time.monotonic()))
                            fetcher.next_request[host] = time.monotonic() + fetcher.interval
                            request = Request(transport_url(row['listing_url']), headers={'User-Agent': 'Mozilla/5.0', 'Cookie': cookie})
                            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                                if urlsplit(response.geturl()).hostname not in ('patuljak.me', 'www.patuljak.me'):
                                    raise ContactFailure('authenticated_redirect_host_invalid')
                                body = response.read(8_000_001)
                            if len(body) > 8_000_000:
                                raise ContactFailure('contact_page_too_large')
                        else:
                            fetcher.max_api = max(0, api_cap - reveal.api_requests)
                            body, final, _ = fetcher.get(row['listing_url'])
                            from daily_scraper import galleries
                            try:
                                identity = galleries.identify(final)
                            except ValueError:
                                raise ContactFailure('contact_redirect_identity_mismatch') from None
                            if identity != (row['source'], str(row['listing_id'])):
                                raise ContactFailure('contact_redirect_identity_mismatch')
                        path.write_bytes(body)
                    from daily_scraper import parse_listing
                    current = parse_listing(body, row['listing_url'], row['source'], row['listing_id'], now())
                    candidates = page_contacts(html.fromstring(body), current)
                    login_required = row['source'] == 'patuljak' and bool(re.search(
                        r'(?:uloguješ|ulogujes|ulogovati|prijavi).*?(?:broj|telefon)', html.fromstring(body).text_content(), re.I | re.S))
                    result = result_from_candidates(candidates, 'login_required' if login_required else 'no_public_number')
            except ContactFailure as exc:
                result = {'status': 'unavailable' if str(exc) in ('reveal_unavailable', 'contact_redirect_identity_mismatch') else 'retry',
                          'candidates': [], 'checked_at': now(), 'error_code': str(exc),
                          'retry_after': exc.retry_after or time.time() + 3600}
                if exc.retry_after:
                    gate['resume_not_before'] = exc.retry_after
                    result['status'] = 'quota_deferred'
            except HTTPError as exc:
                # Authenticated reads do not follow redirects or forward the
                # cookie. A login redirect and a deleted-ad redirect differ.
                try:
                    target = urlsplit(urljoin(row['listing_url'], (exc.headers or {}).get('Location', '')))
                    same_host = target.scheme == 'https' and target.netloc == urlsplit(row['listing_url']).netloc
                except ValueError:
                    target, same_host = urlsplit(''), False
                status, code = 'retry', 'source_http_' + str(exc.code)
                if exc.code in (301, 302, 303, 307, 308) and same_host:
                    if target.path in ('/login.html', '/login', '/prijava'):
                        status, code = 'login_required', 'patuljak_session_required'
                    elif target.path == '/':
                        status, code = 'unavailable', 'contact_redirect_identity_mismatch'
                elif exc.code in (404, 410):
                    status = 'unavailable'
                elif exc.code == 401:
                    status, code = 'login_required', 'patuljak_session_required'
                result = {'status': status, 'candidates': [], 'checked_at': now(),
                          'error_code': code, 'retry_after': time.time() + 3600}
            except Exception as exc:
                from daily_scraper import FetchFailure, UNAVAILABLE_CODES
                code = str(exc) if isinstance(exc, FetchFailure) else 'contact_' + type(exc).__name__
                result = {'status': 'unavailable' if code in UNAVAILABLE_CODES else 'retry',
                          'candidates': [], 'checked_at': now(), 'error_code': code, 'retry_after': time.time() + 3600}
        state['results'][key] = result
        state['records'][key] = apply_result(row, result)
        save(state_path, state)
        if counts['listings_checked'] % 20 == 0:
            print(json.dumps({'event': 'contacts_checked', **counts}), flush=True)
    if counts['listings_checked'] or not state.get('updated_at'):
        state['updated_at'] = now()
    state['last_run_counts'] = dict(counts)
    save(state_path, state)
    return counts


def patch_exports(state, exports, photo_output):
    from daily_scraper import export_run, write_json, csv_text, CSV_FIELDS
    from phone_matches import annotate, legacy_records
    known=list(state['records'].values())+legacy_records()
    for path in sorted(Path(exports).glob('*/listings.json')):
        rows = annotate([apply_result(r, state['results'].get(r['property_key'], {})) for r in json.loads(path.read_text())], known)
        folder = path.parent
        export_run(folder, rows, json.loads((folder / 'errors.json').read_text()),
                   json.loads((folder / 'summary.json').read_text()),
                   json.loads((folder / 'warnings.json').read_text()) if (folder / 'warnings.json').exists() else [])
    path = Path(photo_output) / 'listings_with_photos.json'
    if path.exists():
        rows = annotate([apply_result(r, state['results'].get(r['property_key'], {})) for r in json.loads(path.read_text())], known)
        write_json(path, rows)
        fields = CSV_FIELDS + ['source_image_urls', 'photo_delivery_status', 'ready_image_count', 'held_image_count', 'photo_catalog_url']
        (path.with_suffix('.csv')).write_text(csv_text(fields, rows), encoding='utf-8')


def summary(state):
    records = list(state['records'].values())
    return {'schema_version': 1, 'updated_at': state.get('updated_at'), 'total_listings': len(records),
            'with_phone': sum(bool(r.get('contact_phones')) for r in records),
            'with_single_phone': sum(bool(r.get('phone_e164')) for r in records),
            'by_status': dict(Counter(r.get('phone_collection_status', 'pending') for r in records)),
            'errors': [dict(property_key=k, code=r['error_code']) for k, r in state['results'].items()
                       if r.get('error_code') in ('contact_provider_http_401', 'contact_provider_http_402', 'contact_provider_http_403')
                       or r.get('error_code') in ('contact_TypeError', 'contact_KeyError', 'contact_AttributeError')],
            'estitor_quota': state.get('estitor_gate', {}), 'last_run_counts': state.get('last_run_counts', {}),
            'patuljak_auth': state.get('patuljak_auth', {}),
            'coverage': 'Cumulative daily listings, seeded with the 164 exports from 2026-11-02 and 2026-11-03; original 1797-property import is separate.',
            'contact_identity': 'Advertiser contacts from this listing; not verified private-owner identity.'}


def publish(state, store, output):
    from daily_scraper import CSV_FIELDS, csv_text
    from photo_catalog import encode, sha
    catalog, _ = store.snapshot()
    records = []
    for key, source in sorted(state['records'].items()):
        row = apply_result(source, state['results'].get(key, {}))
        prop = catalog.get('properties', {}).get(key, {})
        photos = sorted(prop.get('images', []), key=lambda r: int(r['image_number']))
        ready = [r['active_url'] for r in photos if r.get('ready_for_listing') and r.get('active_url')]
        row.update(source_image_urls=row.get('source_image_urls', row.get('image_urls', [])), image_urls=ready,
                   image_count=len(ready), photos=photos, ready_image_count=len(ready),
                   held_image_count=sum(not r.get('ready_for_listing') for r in photos),
                   photo_delivery_status=prop.get('photo_pipeline_status', 'pending'),
                   watermark_sample_result=prop.get('watermark_sample', {}).get('result', 'not_checked'))
        records.append(row)
    from phone_matches import annotate, legacy_records, report
    records=annotate(records, list(state['records'].values())+legacy_records())
    matches=report(records)
    stats = {**summary(state), 'phone_matching': {
        'historical_baseline': 'listing_details/listings.json plus cumulative daily contacts',
        'existing_contacts': sum(r['phone_match_status']=='existing_contact' for r in records),
        'new_contacts': sum(r['phone_match_status']=='new_contact' for r in records)}}
    fields = CSV_FIELDS + ['source_image_urls', 'photo_delivery_status', 'ready_image_count', 'held_image_count']
    queue = [{**r, 'retry_after': state['results'].get(r['property_key'], {}).get('retry_after')}
             for r in records if not r.get('contact_phones')]
    assets = {'listings.json': encode(records), 'listings.csv': csv_text(fields, records).encode(),
              'contact_queue.csv': csv_text(['property_key', 'source', 'listing_url', 'phone_collection_status', 'phone_error_code', 'retry_after'], queue).encode(),
              'summary.json': encode(stats), 'phone_updates.json': encode(state['results']),
              'phone_matches.json': encode(matches),
              'phone_matches.csv': csv_text(list(matches[0]) if matches else ['property_key','phone_match_status'],matches).encode()}
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    for name, body in assets.items():
        (output / name).write_bytes(body)
    with zipfile.ZipFile(output / 'listings-with-contacts.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, body in assets.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
    assets['listings-with-contacts.zip'] = (output / 'listings-with-contacts.zip').read_bytes()
    version = sha(encode({k: sha(v) for k, v in assets.items()}))[:24]
    urls = {name: store.put(PREFIX + '/' + version + '/' + name, body,
             'application/zip' if name.endswith('.zip') else 'application/json' if name.endswith('.json') else 'text/csv; charset=utf-8',
             {'owner': OWNER}) for name, body in assets.items()}
    key = PREFIX + '/latest.json'
    head = store.head(key)
    if head and head.get('Metadata', {}).get('owner') != OWNER:
        raise ValueError('Contact pointer owned by another application')
    body = encode({'schema_version': 1, 'assets': urls, 'photo_catalog_index': store.public + '/graden-imports/property-photos/latest.json', **stats})
    store.reserve(len(body))
    store.client.put_object(Bucket=store.bucket, Key=key, Body=body, ContentType='application/json',
                            CacheControl='public, max-age=60', Metadata={'owner': OWNER, 'sha256': sha(body)},
                            **({'IfMatch': head['ETag']} if head else {'IfNoneMatch': '*'}))
    return {'latest_url': store.public + '/' + key, 'assets': urls, **stats}


def patch_remote_daily(state, store):
    """Refresh the existing latest daily feed without changing held/ready photo selections."""
    from photo_catalog import encode, sha
    from daily_scraper import CSV_FIELDS, csv_text
    key = 'graden-imports/property-photos/daily/latest.json'
    head = store.head(key)
    if not head:
        return None
    if head.get('Metadata', {}).get('owner') != 'graden-daily-photos':
        raise ValueError('Daily pointer owned by another application')
    pointer = store.get_json(key, 100_000)
    url = pointer['assets']['listings_with_photos.json']
    if not url.startswith(store.public + '/graden-imports/property-photos/daily/'):
        raise ValueError('Daily listings outside owned delivery prefix')
    rows = store.get_json(url[len(store.public) + 1:])
    updated = [apply_result(r, state['results'][r['property_key']]) if r['property_key'] in state['results'] else r for r in rows]
    if updated == rows:
        return None
    fields = CSV_FIELDS + ['source_image_urls', 'photo_delivery_status', 'ready_image_count', 'held_image_count', 'photo_catalog_url']
    assets = {'listings_with_photos.json': encode(updated), 'listings_with_photos.csv': csv_text(fields, updated).encode()}
    version = sha(encode({k: sha(v) for k, v in assets.items()}))[:24]
    for name, data in assets.items():
        pointer['assets'][name] = store.put('graden-imports/property-photos/daily/' + version + '/' + name, data,
                                            'application/json' if name.endswith('.json') else 'text/csv; charset=utf-8',
                                            {'owner': 'graden-daily-photos'})
    body = encode(pointer)
    store.reserve(len(body))
    store.client.put_object(Bucket=store.bucket, Key=key, Body=body, ContentType='application/json',
                            CacheControl='public, max-age=60', Metadata={'owner': 'graden-daily-photos', 'sha256': sha(body)},
                            IfMatch=head['ETag'])
    return {'updated_records': len(updated), 'latest_url': store.public + '/' + key}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=HERE / 'runtime/contact-state.json')
    parser.add_argument('--exports', type=Path, default=HERE / 'exports')
    parser.add_argument('--backfill', type=Path, default=HERE / 'contact_backfill.json')
    parser.add_argument('--cache', type=Path, default=HERE / 'runtime/contact-pages')
    parser.add_argument('--photo-output', type=Path, default=HERE / 'photo_exports')
    parser.add_argument('--output', type=Path, default=HERE / 'contact_exports')
    parser.add_argument('--api-key-file', type=Path)
    parser.add_argument('--max-listings', type=int, default=300)
    parser.add_argument('--max-reveals', type=int, default=10)
    parser.add_argument('--max-api-requests', type=int, default=20)
    parser.add_argument('--max-runtime-seconds', type=int, default=600)
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args(argv)
    if min(args.max_listings, args.max_reveals, args.max_api_requests, args.max_runtime_seconds) < 0:
        parser.error('Limits must be nonnegative')
    import fcntl
    args.state.parent.mkdir(parents=True, exist_ok=True)
    with args.state.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load_inputs(args.state, args.exports, args.backfill)
        if args.collect:
            from daily_scraper import Fetcher
            key = args.api_key_file.read_text().strip() if args.api_key_file else os.getenv('BRIGHTDATA_API_KEY', '')
            fetcher = Fetcher(key, os.getenv('BRIGHTDATA_ZONE', 'esstitor_site_phone_scraper'),
                              'auto' if key else 'direct', args.max_api_requests,
                              browser_zone=os.getenv('BRIGHTDATA_BROWSER_ZONE', 'estitorscraper2'))
            reveal = RevealClient(key, os.getenv('BRIGHTDATA_ZONE', 'esstitor_site_phone_scraper'), args.max_api_requests)
            cookie = os.getenv('PATULJAK_COOKIE', '')
            pending_patuljak = any(row.get('source') == 'patuljak' and not row.get('contact_phones') and
                                  state['results'].get(k, {}).get('status', 'pending') in ('pending', 'retry', 'login_required')
                                  for k, row in state['records'].items())
            if args.max_listings and pending_patuljak:
                from patuljak_auth import resolve_cookie
                cookie, auth_status = resolve_cookie(os.environ)
                state['patuljak_auth'] = {**auth_status, 'checked_at': now()}
                if os.getenv('GITHUB_ACTIONS') == 'true' and auth_status.get('status') in (
                        'authentication_deferred', 'textbee_pairing_required', 'session_expired_auto_login_disabled'):
                    print('::warning::Patuljak authentication: ' + auth_status['status'], flush=True)
            collect(state, args.state, fetcher, reveal, args.cache, args.max_listings, args.max_reveals,
                    args.max_runtime_seconds, cookie)
        save(args.state, state)
        patch_exports(state, args.exports, args.photo_output)
        args.output.mkdir(parents=True, exist_ok=True)
        save(args.output / 'summary.json', summary(state))
        if args.publish:
            import boto3
            from botocore.config import Config
            from photo_catalog import R2Store
            config = json.loads((HERE.parent / 'property_photo_pipeline/r2_config.json').read_text())
            client = boto3.client('s3', endpoint_url=config['endpoint_url'], region_name='auto',
                                  aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
                                  aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
                                  config=Config(signature_version='s3v4', retries={'max_attempts': 3}))
            store = R2Store(client, config['bucket_name'], config['public_base_url'])
            result = publish(state, store, args.output)
            result['daily_feed_refresh'] = patch_remote_daily(state, store)
            save(args.output / 'delivery.json', result)
        print(json.dumps(summary(state)), flush=True)
    return 2 if summary(state)['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
