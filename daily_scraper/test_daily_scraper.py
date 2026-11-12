import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_scraper as scraper
from brightdata_browser import BrowserAPI, BrowserFailure
from url_utils import transport_url


URL = 'https://patuljak.me/oglas/stan--123'
BODY = b'''<html lang="me"><head><link rel="canonical" href="https://patuljak.me/oglas/stan--123"></head>
<body><h1>Prodaja stan Podgorica</h1><a href="tel:+38269123456">Phone</a>
<div itemscope itemtype="https://schema.org/ImageGallery"><a href="/images/123.jpg">photo</a></div>
<div><h2>Detaljan opis</h2><p>Original source text.</p></div></body></html>'''


class DailyTests(unittest.TestCase):
    def test_realitica_search_follows_observed_current_paginator(self):
        from urllib.parse import urlsplit, parse_qs
        parts = urlsplit(scraper.search_url('realitica', 'Prodaja', 1))
        self.assertEqual(parts.path, '/')
        self.assertEqual(parse_qs(parts.query)['pState'], ['Crna Gora'])
        self.assertEqual(parse_qs(parts.query)['cur_page'], ['1'])

    def small_run(self, root, found, fetch, extra=()):
        root = Path(root)
        baseline = root / 'baseline.json'; baseline.write_text('{"listings": {}}')
        args = ['--state', str(root / 'state.json'), '--baseline', str(baseline), '--output', str(root / 'exports'), *extra]
        with patch.object(scraper, 'discover', return_value=(found, [], [])), patch.object(scraper.Fetcher, 'get', side_effect=fetch):
            code = scraper.main(args)
        summary = json.loads((root / 'exports/latest.json').read_text())['summary']
        state = json.loads((root / 'state.json').read_text())
        return code, summary, state

    def test_request_limit_defers_paid_sources_and_continues_direct_source(self):
        second = URL.replace('--123', '--124')
        found = {'realitica_100': 'https://www.realitica.com/hr/listing/100', 'patuljak_123': URL,
                 'estitor_100': 'https://estitor.com/me/id-100', 'patuljak_124': second}
        def fetch(url, **kwargs):
            if 'patuljak.me' not in url: raise scraper.FetchFailure('brightdata_request_cap_reached')
            return (BODY.replace(b'--123', b'--124') if url == second else BODY), url, 'direct_https'
        with tempfile.TemporaryDirectory() as root:
            code, summary, state = self.small_run(root, found, fetch, ['--max-listings', '2'])
        self.assertEqual(code, 0)
        self.assertEqual(summary['captured_listings'], 2)
        self.assertEqual(summary['errors'], 0)
        self.assertEqual(summary['deferred_by_request_limit'], 2)
        self.assertEqual(state['listings']['realitica_100']['attempts'], 0)
        self.assertEqual(state['listings']['estitor_100']['status'], 'pending')

    def test_authentication_failure_still_fails_workflow(self):
        def fetch(url, **kw): raise scraper.FetchFailure('brightdata_http_401')
        with tempfile.TemporaryDirectory() as root:
            code, summary, _ = self.small_run(root, {'patuljak_123': URL}, fetch)
        self.assertEqual(code, 2)
        self.assertEqual(summary['errors'], 1)

    def test_expired_listing_is_recorded_without_failing_successful_discovery(self):
        def fetch(url, **kw): raise scraper.FetchFailure('browser_source_http_404')
        with tempfile.TemporaryDirectory() as root:
            code, summary, state = self.small_run(root, {'patuljak_123': URL}, fetch)
        self.assertEqual(code, 0)
        self.assertEqual(summary['warnings'], 1)
        self.assertEqual(state['listings']['patuljak_123']['status'], 'unavailable')

    def test_complete_capture_outage_still_reports_failure_and_preserves_retry(self):
        def fetch(url, **kw): raise scraper.FetchFailure('browser_challenge_not_resolved')
        with tempfile.TemporaryDirectory() as root:
            code, summary, state = self.small_run(root, {'patuljak_123': URL}, fetch)
        self.assertEqual(code, 2)
        self.assertEqual(summary['errors'], 1)
        self.assertEqual(state['listings']['patuljak_123']['status'], 'retry')

    def test_runtime_budget_exports_and_preserves_pending_queue(self):
        with tempfile.TemporaryDirectory() as root, patch.object(scraper.time, 'monotonic', side_effect=[0, 2]):
            code, summary, state = self.small_run(root, {'patuljak_123': URL}, lambda *a: self.fail('Fetch after deadline'),
                                                ['--max-runtime-seconds', '1'])
        self.assertEqual(code, 0)
        self.assertIn('runtime_limit_reached', summary['limits_reached'])
        self.assertEqual(state['listings']['patuljak_123']['status'], 'pending')

    def test_discovery_cap_is_a_stop_reason_without_errors(self):
        fetcher = MagicMock()
        fetcher.get.side_effect = scraper.FetchFailure('brightdata_request_cap_reached')
        found, reports, errors = scraper.discover(fetcher, ['patuljak'], 3, {}, 0)
        self.assertEqual(errors, [])
        self.assertEqual(reports[0]['stop_reason'], 'brightdata_request_cap_reached')

    def test_unicode_paths_and_queries_are_encoded_without_double_escaping(self):
        url = 'https://patuljak.me/oglas/kuća-žabljak--86004?grad=Žabljak&x=a%20b#photo'
        encoded = transport_url(url)
        self.assertTrue(encoded.isascii())
        self.assertIn('ku%C4%87a-%C5%BEabljak', encoded)
        self.assertIn('grad=%C5%BDabljak', encoded)
        self.assertNotIn('%2520', encoded)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = BODY
        response.__enter__.return_value.geturl.return_value = encoded
        with patch.object(scraper, 'urlopen', return_value=response) as send:
            scraper.Fetcher('', 'zone', mode='direct').get(url)
            self.assertTrue(send.call_args.args[0].full_url.isascii())

    def test_link_identity_deduplicates_and_rejects_other_hosts(self):
        body = b'<a href="/oglas/stan--123">a</a><a href="/oglas/stan--123?x=2">b</a><a href="https://evil.test/oglas/a--5">c</a>'
        self.assertEqual(scraper.listing_links(body, URL, 'patuljak'), {'patuljak_123': URL})

    def test_canonical_prevents_wrong_property_assignment(self):
        with self.assertRaisesRegex(scraper.FetchFailure, 'canonical_listing_mismatch'):
            scraper.parse_listing(BODY, URL, 'patuljak', '124', scraper.now())

    def test_source_text_and_sampling_are_not_fabricated(self):
        r = scraper.parse_listing(BODY, URL, 'patuljak', '123', scraper.now())
        self.assertEqual(r['description_original'], 'Original source text.')
        self.assertEqual(r['phone_e164'], '+38269123456')
        self.assertEqual(r['watermark_sample_result'], 'not_checked')
        self.assertEqual(r['watermark_sample_url'], 'https://patuljak.me/images/123.jpg')
        self.assertIn('price', r['missing_core_fields'])
        self.assertNotIn('latitude', r)

    def test_challenge_is_not_a_listing(self):
        with self.assertRaisesRegex(scraper.FetchFailure, 'listing_body_missing'):
            scraper.parse_listing(b'<html>Checking your browser</html>', URL, 'patuljak', '123', scraper.now())

    def test_realitica_thumbnail_scoped_to_exact_listing(self):
        body = b'<html><div id="listing_body"><h2>Stan</h2></div><img src="/c_images/thumb_123_1_photo.jpeg"><img src="/c_images/thumb_124_1_other.jpeg"></html>'
        r = scraper.parse_listing(body, 'https://www.realitica.com/hr/listing/123', 'realitica', '123', scraper.now())
        self.assertEqual(r['image_urls'], ['https://www.realitica.com/c_images/thumb_123_1_photo.jpeg'])
        self.assertIn('only_low_resolution_preview_exposed', r['quality_flags'])

    def test_state_corruption_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(ValueError):
                scraper.load_state(path, Path(tmp) / 'missing-baseline')

    def test_csv_formula_escaped_json_retains_source(self):
        self.assertIn("'=HYPERLINK", scraper.csv_text(['title'], [{'title': '=HYPERLINK("x")'}]))

    def test_repeated_pagination_is_reported(self):
        class Fake:
            mode = 'direct'
            def get(self, url):
                return b'<a href="/oglas/stan--123">x</a>', url, 'direct_https'
        found, reports, errors = scraper.discover(Fake(), ['patuljak'], 10, {}, 0)
        self.assertEqual(len(found), 1)
        self.assertEqual(reports[0]['stop_reason'], 'repeated_page_requires_review')
        self.assertEqual(errors, [])

    def run_mocked(self, directory, body=BODY, fail_export=False):
        directory = Path(directory)
        baseline = directory / 'baseline.json'
        baseline.write_text(json.dumps({'listings': {}}))
        args = ['--sources', 'patuljak', '--mode', 'direct', '--state', str(directory / 'state.json'),
                '--baseline', str(baseline), '--output', str(directory / 'exports'), '--max-listings', '1']
        with patch.object(scraper, 'discover', return_value=({'patuljak_123': URL}, [], [])), \
             patch.object(scraper.Fetcher, 'get', return_value=(body, URL, 'direct_https')):
            if fail_export:
                with patch.object(scraper, 'export_run', side_effect=OSError('simulated_disk_failure')):
                    with self.assertRaises(OSError):
                        scraper.main(args)
            else:
                scraper.main(args)
        return json.loads((directory / 'state.json').read_text())

    def test_resume_deduplicates_successes(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.run_mocked(tmp)
            self.assertEqual(state['listings']['patuljak_123']['status'], 'exported')
            self.run_mocked(tmp)
            latest = json.loads((Path(tmp) / 'exports/latest.json').read_text())
            self.assertEqual(latest['summary']['captured_listings'], 0)

    def test_export_failure_recovers_capture_next_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.run_mocked(tmp, fail_export=True)
            self.assertEqual(state['listings']['patuljak_123']['status'], 'captured_unexported')
            self.run_mocked(tmp)
            latest = json.loads((Path(tmp) / 'exports/latest.json').read_text())
            self.assertEqual(latest['summary']['captured_listings'], 1)

    def test_failed_capture_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = self.run_mocked(tmp, body=b'<html>challenge</html>')
            self.assertEqual(state['listings']['patuljak_123']['status'], 'retry')
            state = self.run_mocked(tmp)
            self.assertEqual(state['listings']['patuljak_123']['status'], 'exported')

    def test_api_cap_prevents_request(self):
        fetcher = scraper.Fetcher('placeholder-test-key', 'test-zone', mode='always', max_api=0)
        with patch.object(scraper, 'urlopen') as send:
            with self.assertRaisesRegex(scraper.FetchFailure, 'brightdata_request_cap_reached'):
                fetcher.get(URL)
            send.assert_not_called()

    def test_api_auth_only_sent_to_provider(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, limit): return json.dumps({'status_code': 200, 'body': '<html>x</html>'}).encode()
        fetcher = scraper.Fetcher('placeholder-test-key', 'test-zone', mode='always')
        with patch.object(scraper, 'urlopen', return_value=Response()) as send:
            fetcher.get(URL)
            req = send.call_args.args[0]
            self.assertEqual(req.full_url, 'https://api.brightdata.com/request')
            payload = json.loads(req.data)
            self.assertEqual(payload['url'], URL)
            self.assertNotIn('placeholder-test-key', req.data.decode())

    def test_proxy_403_uses_browser_but_429_does_not(self):
        class Response:
            status = 403
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, limit): return json.dumps({'status_code': self.status, 'body': 'blocked'}).encode()
        fetcher = scraper.Fetcher('test-key', 'dc-zone', mode='always', browser_zone='existing-browser')
        with patch.object(scraper, 'urlopen', return_value=Response()), \
             patch.object(fetcher.browser, 'get', return_value=(BODY, URL, 'brightdata_browser_api')) as browser:
            self.assertEqual(fetcher.get(URL)[2], 'brightdata_browser_api')
            browser.assert_called_once()
        Response.status = 429
        fetcher = scraper.Fetcher('test-key', 'dc-zone', mode='always', browser_zone='existing-browser')
        with patch.object(scraper, 'urlopen', return_value=Response()), patch.object(fetcher.browser, 'get') as browser:
            with self.assertRaisesRegex(scraper.FetchFailure, 'proxied_source_http_429'):
                fetcher.get(URL)
            browser.assert_not_called()

    def test_browser_retry_succeeds_and_reuses_working_route(self):
        fetcher = scraper.Fetcher('test-key', 'dc', mode='always', max_api=4, browser_zone='browser')
        result = (BODY, URL, 'brightdata_browser_api')
        with patch.object(fetcher.browser, 'get', side_effect=[BrowserFailure('browser_navigation_aborted'), result, result]) as get, \
             patch.object(scraper.time, 'sleep'), patch.object(scraper, 'urlopen') as http:
            self.assertEqual(fetcher.browser_get(URL), result)
            self.assertEqual(fetcher.get(URL), result)
            self.assertEqual(get.call_count, 3)
            http.assert_not_called()
            self.assertEqual(fetcher.counts['browser_retries'], 1)

    def test_browser_retry_respects_request_cap(self):
        fetcher = scraper.Fetcher('test-key', 'dc', mode='browser', max_api=1, browser_zone='browser')
        with patch.object(fetcher.browser, 'get', side_effect=BrowserFailure('browser_navigate_failed')) as get, \
             patch.object(scraper.time, 'sleep'):
            with self.assertRaisesRegex(scraper.FetchFailure, 'request_cap_reached'):
                fetcher.get(URL)
            self.assertEqual(get.call_count, 1)

    def test_browser_rate_limit_is_not_retried(self):
        fetcher = scraper.Fetcher('test-key', 'dc', mode='browser', max_api=4, browser_zone='browser')
        with patch.object(fetcher.browser, 'get', side_effect=BrowserFailure('browser_source_http_429')) as get:
            with self.assertRaisesRegex(scraper.FetchFailure, 'browser_source_http_429'):
                fetcher.get(URL)
            self.assertEqual(get.call_count, 1)
            with self.assertRaisesRegex(scraper.FetchFailure, 'source_rate_limited'):
                fetcher.get(URL)

    def test_http_and_browser_share_page_request_cap(self):
        fetcher = scraper.Fetcher('test-key', 'dc', mode='browser', max_api=1, browser_zone='browser')
        fetcher.counts['brightdata_requests'] = 1
        with patch.object(fetcher.browser, 'get') as browser:
            with self.assertRaisesRegex(scraper.FetchFailure, 'request_cap_reached'):
                fetcher.get(URL)
            browser.assert_not_called()

    def test_browser_credentials_resolved_from_key_and_cached(self):
        api = BrowserAPI('test-key', 'existing-browser', scraper.ALLOWED_HOSTS)
        with patch.object(api, 'account_get', side_effect=[{'customer': 'hl_test'}, {'plan': {'type': 'browser_api'}, 'password': ['private-password']}]) as get:
            first = api.auth_headers()
            self.assertTrue(first['Authorization'].startswith('Basic '))
            self.assertEqual(first, api.auth_headers())
            self.assertEqual(get.call_count, 2)

    def test_browser_error_redacts_credentials_and_stops_refused_connections(self):
        api = BrowserAPI('sensitive-api-key', 'browser', scraper.ALLOWED_HOSTS)
        manager = MagicMock()
        manager.__enter__.return_value.chromium.connect_over_cdp.side_effect = RuntimeError('ECONNREFUSED secret-password sensitive-api-key')
        with patch.object(api, 'auth_headers', return_value={'Authorization': 'Basic sensitive-value'}), \
             patch('playwright.sync_api.sync_playwright', return_value=manager):
            with self.assertRaisesRegex(BrowserFailure, '^browser_network_port_refused$'):
                api.get(URL)
            with self.assertRaisesRegex(BrowserFailure, '^brightdata_browser_unavailable$'):
                api.get(URL)
            self.assertEqual(manager.__enter__.return_value.chromium.connect_over_cdp.call_count, 1)

    def test_browser_rejects_datacenter_zone_credentials(self):
        api = BrowserAPI('key', 'dc', scraper.ALLOWED_HOSTS)
        with patch.object(api, 'account_get', side_effect=[{'customer':'hl_test'}, {'plan':{'type':'static'},'password':['secret']}]):
            with self.assertRaisesRegex(BrowserFailure, 'not_browser_api'):
                api.auth_headers()

    def test_browser_image_returns_original_response_bytes(self):
        api = BrowserAPI('key', 'browser', scraper.ALLOWED_HOSTS)
        manager = MagicMock()
        browser = manager.__enter__.return_value.chromium.connect_over_cdp.return_value
        page = browser.new_page.return_value
        page.url = URL
        response = page.goto.return_value
        response.status = 200
        response.headers = {'content-type': 'image/jpeg'}
        response.body.return_value = b'original-image-bytes'
        with patch.object(api, 'auth_headers', return_value={'Authorization': 'Basic masked'}), \
             patch('playwright.sync_api.sync_playwright', return_value=manager):
            self.assertEqual(api.get_image(URL)[0], b'original-image-bytes')
        page.route.assert_not_called()
        browser.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
