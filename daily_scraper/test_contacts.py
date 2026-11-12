import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from collections import Counter

from lxml import html

sys.path.insert(0, str(Path(__file__).resolve().parent))
import contact_enrichment as c
import daily_scraper as scraper


def record(source='estitor', ident='123', description=''):
    urls = {'estitor': f'https://estitor.com/me/nekretnine/namjena-prodaja/tip-stan/grad-budva/id-{ident}',
            'realitica': f'https://www.realitica.com/hr/listing/{ident}',
            'patuljak': f'https://patuljak.me/oglas/stan--{ident}'}
    return {'property_key': source + '_' + ident, 'source': source, 'listing_id': ident,
            'listing_url': urls[source], 'description_original': description, 'image_urls': ['https://example.com/image.jpg'],
            'field_sources': {}, 'quality_flags': [], 'contact_phones': [], 'missing_core_fields': ['city']}


class ContactTests(unittest.TestCase):
    def test_normalize_local_and_international_and_exclude_platform(self):
        self.assertEqual(c.normalize_phone('069/123-456', True), '+38269123456')
        self.assertEqual(c.normalize_phone('00382 20 123 456'), '+38220123456')
        self.assertIsNone(c.normalize_phone('+38269257004'))
        self.assertIsNone(c.normalize_phone('123456789'))
        self.assertIsNone(c.normalize_phone('+38269123456789'))

    def test_description_numbers_keep_distinct_candidates_and_avoid_area_price_ids(self):
        self.assertEqual(c.text_phones('70m2, 120000 EUR. Tel: 069 123 456, +382 68 654 321.'),
                         ['+38268654321', '+38269123456'])
        self.assertEqual(c.text_phones('ID 649875; 70m². Cijena 123456789 EUR.'), [])

    def test_repeated_same_number_is_not_ambiguous(self):
        row = record('patuljak', description='069 123 456 i +382 69 123 456')
        result = c.result_from_candidates(c.page_contacts(html.fromstring('<html/>'), row))
        self.assertEqual(result['status'], 'found')
        self.assertEqual(c.apply_result(row, result)['phone_e164'], '+38269123456')

    def test_realitica_button_handler_is_scoped_to_advertiser(self):
        row = record('realitica')
        root = html.fromstring('''<html><div id="aboutAuthor"><button id="j123">Phone</button></div>
          <script>$("#j123").click(function() { var p = "38269123456"; $(this).html(p); });
          $("#j999").click(function() { var p = "38268999999"; });</script>
          <footer><a href="tel:+38267111111">Support</a></footer></html>''')
        candidates = c.page_contacts(root, row)
        self.assertEqual([v['phone_e164'] for v in candidates], ['+38269123456'])
        self.assertEqual(candidates[0]['source'], 'page:realitica_phone_button')

    def test_patuljak_does_not_collect_footer_popup_or_related_numbers(self):
        row = record('patuljak')
        root = html.fromstring('''<html><div class="product_full"><a href="tel:069123456">Seller</a>
          <div class="related"><a href="tel:+38268999999">Other ad</a></div></div>
          <div class="popup"><p>Tel 067 999 999</p></div><footer><a href="tel:+38267111111">Help</a></footer></html>''')
        self.assertEqual([v['phone_e164'] for v in c.page_contacts(root, row)], ['+38269123456'])

    def test_realitica_alternative_handler_preserves_multiple_formatted_numbers(self):
        row = record('realitica')
        root = html.fromstring('''<html><div id="aboutAuthor"><button id="f01">Telefonski broj</button>
          <script>$("#f01").click(function() { var p = "+382 69 123 456 viber 068/654-321"; $(this).html(p); });</script>
          </div></html>''')
        result = c.result_from_candidates(c.page_contacts(root, row))
        self.assertEqual(result['status'], 'multiple_numbers')
        self.assertEqual({v['phone_e164'] for v in result['candidates']}, {'+38269123456', '+38268654321'})

    def test_multiple_numbers_preserved_without_choosing_owner_or_primary(self):
        row = record(description='Call 069123456 or 068654321')
        result = c.result_from_candidates(c.page_contacts(html.fromstring('<html/>'), row))
        updated = c.apply_result(row, result)
        self.assertEqual(updated['phone_collection_status'], 'multiple_numbers')
        self.assertIsNone(updated['phone_e164'])
        self.assertEqual(updated['missing_core_fields'], ['city'])
        self.assertNotIn('owner_verified', updated)
        self.assertEqual(row['contact_phones'], [])

    def test_existing_contacts_survive_failed_enrichment(self):
        row = record()
        row['contact_phones'] = ['+38269123456']
        row['phone_e164'] = '+38269123456'
        updated = c.apply_result(row, {'status': 'retry', 'candidates': []})
        self.assertEqual(updated['phone_e164'], row['phone_e164'])

    def response(self, data):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(data).encode()
        return response

    def test_reveal_payload_is_listing_specific_and_has_no_outreach(self):
        with patch.object(c, 'urlopen', return_value=self.response({'phone': '+38269123456'})) as send:
            c.RevealClient().reveal(record())
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, c.REVEAL_URL)
        self.assertEqual(json.loads(req.data)['adId'], 123)
        self.assertEqual(json.loads(req.data)['pageUrl'], record()['listing_url'])

    def test_quota_never_uses_proxy_fallback(self):
        error = HTTPError(c.REVEAL_URL, 403, '', {}, io.BytesIO(b'{"code":"DAILY_LIMIT_EXCEEDED"}'))
        with patch.object(c, 'urlopen', side_effect=error) as send:
            with self.assertRaises(c.ContactFailure) as caught:
                c.RevealClient('secret').reveal(record())
        self.assertEqual(str(caught.exception), 'estitor_quota_or_rate_limit')
        self.assertIsNotNone(caught.exception.retry_after)
        self.assertEqual(send.call_count, 1)

    def test_ambiguous_post_failure_is_not_replayed(self):
        with patch.object(c, 'urlopen', side_effect=URLError('sensitive details')) as send:
            with self.assertRaisesRegex(c.ContactFailure, '^reveal_transport_unknown$'):
                c.RevealClient('secret').reveal(record())
        self.assertEqual(send.call_count, 1)

    def test_unknown_application_403_is_not_replayed_through_another_route(self):
        error = HTTPError(c.REVEAL_URL, 403, '', {}, io.BytesIO(b'{"message":"not permitted"}'))
        with patch.object(c, 'urlopen', side_effect=error) as send:
            with self.assertRaisesRegex(c.ContactFailure, '^reveal_http_403$'):
                c.RevealClient('secret').reveal(record())
        self.assertEqual(send.call_count, 1)

    def test_html_block_can_use_one_bounded_brightdata_post(self):
        error = HTTPError(c.REVEAL_URL, 403, '', {}, io.BytesIO(b'<html>CloudFront request blocked</html>'))
        reply = {'status_code': 200, 'body': json.dumps({'phone': '+38269123456', 'remainingHour': 4})}
        with patch.object(c, 'urlopen', side_effect=[error, self.response(reply)]) as send:
            result = c.RevealClient('secret', max_api=1).reveal(record())
        self.assertEqual(result['phone'], '+38269123456')
        self.assertEqual(send.call_count, 2)
        req = send.call_args.args[0]
        self.assertEqual(req.full_url, 'https://api.brightdata.com/request')
        self.assertEqual(json.loads(req.data)['url'], c.REVEAL_URL)
        self.assertEqual(json.loads(json.loads(req.data)['body'])['adId'], 123)

    def state(self, *rows):
        return {'schema_version': 1, 'records': {r['property_key']: r for r in rows}, 'results': {}, 'estitor_gate': {}}

    def worker(self, state, root, reveal=None, **kwargs):
        fetcher = MagicMock(max_api=20, counts=Counter(), next_request={}, interval=.7)
        reveal = reveal or MagicMock(api_requests=0)
        with patch.object(c.time, 'sleep'):
            c.collect(state, root / 'state.json', fetcher, reveal, root / 'cache', **kwargs)
        return fetcher, reveal

    def test_hourly_quota_checkpoints_success_and_defers_remaining(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.state(record(ident='123'), record(ident='124'))
            reveal = MagicMock(api_requests=0)
            reveal.reveal.return_value = {'phone': '+38269123456', 'remainingHour': 0, 'remainingDay': 4}
            self.worker(state, root, reveal)
            saved = json.loads((root / 'state.json').read_text())
            self.assertEqual(reveal.reveal.call_count, 1)
            self.assertEqual(saved['records']['estitor_123']['phone_e164'], '+38269123456')
            self.assertEqual(saved['results']['estitor_124']['status'], 'quota_deferred')
            self.assertGreater(saved['estitor_gate']['resume_not_before'], c.time.time())
            self.worker(state, root, reveal)
            self.assertEqual(reveal.reveal.call_count, 1)

    def test_pending_reveals_resume_after_cooldown_without_refetching_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = self.state(record(ident='123'), record(ident='124'))
            reveal = MagicMock(api_requests=0)
            reveal.reveal.return_value = {'phone': '+38269123456', 'remainingHour': 0, 'remainingDay': 4}
            self.worker(state, root, reveal)
            state['estitor_gate']['resume_not_before'] = 0
            self.worker(state, root, reveal)
            self.assertEqual(reveal.reveal.call_count, 2)
            self.assertTrue(all(r.get('contact_phones') for r in state['records'].values()))

    def test_description_contact_requires_no_page_or_reveal_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(record('patuljak', description='Pozovi 069/123-456'))
            fetcher, reveal = self.worker(state, Path(directory))
            fetcher.get.assert_not_called()
            reveal.reveal.assert_not_called()
            self.assertEqual(state['records']['patuljak_123']['phone_e164'], '+38269123456')

    def test_authenticated_login_redirect_is_not_reported_as_a_removed_ad(self):
        with tempfile.TemporaryDirectory() as directory:
            row=record('patuljak');state=self.state(row);opener=MagicMock()
            opener.open.side_effect=HTTPError(row['listing_url'],302,'',{'Location':'/login.html'},io.BytesIO())
            state['results'][row['property_key']]={'status':'retry','error_code':'contact_HTTPError',
                                                  'retry_after':c.time.time()+3600}
            with patch.object(c,'build_opener',return_value=opener):
                self.worker(state,Path(directory),cookie='session=test')
            self.assertEqual(state['results'][row['property_key']]['status'],'login_required')
            self.assertEqual(state['results'][row['property_key']]['error_code'],'patuljak_session_required')
            self.assertEqual(opener.open.call_count,1)

    def test_authenticated_removed_ad_redirect_stays_on_original_host_without_following(self):
        for location,expected in [('/', 'unavailable'),('https://example.org/', 'retry')]:
            with self.subTest(location=location),tempfile.TemporaryDirectory() as directory:
                row=record('patuljak');state=self.state(row);opener=MagicMock()
                opener.open.side_effect=HTTPError(row['listing_url'],302,'',{'Location':location},io.BytesIO())
                with patch.object(c,'build_opener',return_value=opener):
                    self.worker(state,Path(directory),cookie='session=test')
                self.assertEqual(state['results'][row['property_key']]['status'],expected)
                self.assertEqual(opener.open.call_count,1)

    def test_classified_authenticated_failures_keep_their_retry_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            row=record('patuljak');state=self.state(row)
            state['results'][row['property_key']]={'status':'retry','error_code':'source_http_403',
                                                  'retry_after':c.time.time()+3600}
            with patch.object(c,'build_opener') as opener:
                self.worker(state,Path(directory),cookie='session=test')
            opener.assert_not_called()

    def test_quota_deferred_ads_do_not_starve_an_accessible_source(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(record(ident='123'), record(ident='124'),
                               record('patuljak', description='Pozovi 069123456'))
            state['estitor_gate']['resume_not_before'] = c.time.time() + 3600
            fetcher, reveal = self.worker(state, Path(directory), max_listings=1)
            self.assertEqual(state['records']['patuljak_123']['phone_e164'], '+38269123456')
            self.assertEqual(state['last_run_counts']['listings_checked'], 1)
            self.assertEqual(state['last_run_counts']['quota_or_reveal_limit_deferred'], 2)
            reveal.reveal.assert_not_called()
            fetcher.get.assert_not_called()

    def test_time_budget_leaves_records_pending_without_network_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(record())
            fetcher, reveal = self.worker(state, Path(directory), max_seconds=0)
            reveal.reveal.assert_not_called()
            fetcher.get.assert_not_called()
            self.assertEqual(state['results'], {})

    def test_identity_mismatch_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record()
            row['listing_url'] = row['listing_url'].replace('id-123', 'id-999')
            backfill = root / 'backfill.json'
            backfill.write_text(json.dumps([row]))
            with self.assertRaisesRegex(ValueError, 'URL identity mismatch'):
                c.load_inputs(root / 'state.json', root, backfill)

    def test_export_and_zip_receive_phone_updates_without_changing_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row = record(description='Call 069123456')
            folder = root / 'exports/run1'
            scraper.export_run(folder, [row], [], {'status': 'completed'})
            result = c.result_from_candidates(c.page_contacts(html.fromstring('<html/>'), row))
            state = self.state(row)
            state['results'][row['property_key']] = result
            c.patch_exports(state, root / 'exports', root / 'no-photo-output')
            rows = json.loads((folder / 'listings.json').read_text())
            self.assertEqual(rows[0]['phone_e164'], '+38269123456')
            self.assertEqual(rows[0]['image_urls'], row['image_urls'])
            import zipfile
            with zipfile.ZipFile(root / 'exports/run1.zip') as archive:
                self.assertEqual(json.loads(archive.read('listings.json'))[0]['phone_e164'], '+38269123456')

    def test_r2_publication_joins_only_ready_images_and_preserves_source_urls(self):
        from test_photos import MemoryS3
        from photo_catalog import R2Store
        with tempfile.TemporaryDirectory() as directory:
            state = self.state(record(description='Call 069123456'))
            client = MemoryS3()
            store = R2Store(client, 'bucket', 'https://cdn.example.com')
            prop = {'images': [{'image_number': 1, 'ready_for_listing': True, 'active_url': 'https://cdn.example.com/ready.webp'},
                               {'image_number': 2, 'ready_for_listing': False, 'active_url': 'https://cdn.example.com/held.webp'}]}
            with patch.object(store, 'snapshot', return_value=({'properties': {'estitor_123': prop}}, 'etag')):
                result = c.publish(state, store, directory)
            key = result['assets']['listings.json'].split('https://cdn.example.com/')[1]
            row = json.loads(client.objects[key]['Body'])[0]
            self.assertEqual(row['image_urls'], ['https://cdn.example.com/ready.webp'])
            self.assertEqual(row['source_image_urls'], record()['image_urls'])
            self.assertIn(c.PREFIX + '/latest.json', client.objects)
            first_zip = (Path(directory) / 'listings-with-contacts.zip').read_bytes()
            with patch.object(store, 'snapshot', return_value=({'properties': {'estitor_123': prop}}, 'etag')):
                repeated = c.publish(state, store, directory)
            self.assertEqual(repeated['assets'], result['assets'])
            self.assertEqual((Path(directory) / 'listings-with-contacts.zip').read_bytes(), first_zip)

    def test_remote_daily_refresh_preserves_image_selection_and_other_asset_urls(self):
        from test_photos import MemoryS3
        from photo_catalog import R2Store, encode
        with tempfile.TemporaryDirectory() as directory:
            row = record()
            row.update(image_urls=['https://cdn.example.com/accepted-cleaned.webp'], ready_image_count=1)
            state = self.state(row)
            state['results'][row['property_key']] = c.result_from_candidates(
                [{'phone_e164': '+38269123456', 'source': 'estitor:listing_phone_reveal', 'listing_url': row['listing_url']}])
            client = MemoryS3()
            store = R2Store(client, 'bucket', 'https://cdn.example.com')
            listing_key = 'graden-imports/property-photos/daily/old/listings_with_photos.json'
            pointer_key = 'graden-imports/property-photos/daily/latest.json'
            other = 'https://cdn.example.com/unchanged-summary.json'
            client.put_object(Bucket='bucket', Key=listing_key, Body=encode([row]), Metadata={})
            client.put_object(Bucket='bucket', Key=pointer_key, Body=encode({'assets': {
                'listings_with_photos.json': store.public + '/' + listing_key, 'summary.json': other}}),
                Metadata={'owner': 'graden-daily-photos'})
            c.patch_remote_daily(state, store)
            pointer = json.loads(client.objects[pointer_key]['Body'])
            key = pointer['assets']['listings_with_photos.json'].split(store.public + '/')[1]
            updated = json.loads(client.objects[key]['Body'])[0]
            self.assertEqual(updated['image_urls'], row['image_urls'])
            self.assertEqual(updated['phone_e164'], '+38269123456')
            self.assertEqual(pointer['assets']['summary.json'], other)
            self.assertIsNone(c.patch_remote_daily(state, store))


if __name__ == '__main__':
    unittest.main()
