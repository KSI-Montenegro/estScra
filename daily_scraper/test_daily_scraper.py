import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import daily_scraper as scraper


URL = 'https://patuljak.me/oglas/stan--123'
BODY = b'''<html lang="me"><head><link rel="canonical" href="https://patuljak.me/oglas/stan--123"></head>
<body><h1>Prodaja stan Podgorica</h1><a href="tel:+38269123456">Phone</a>
<div itemscope itemtype="https://schema.org/ImageGallery"><a href="/images/123.jpg">photo</a></div>
<div><h2>Detaljan opis</h2><p>Original source text.</p></div></body></html>'''


class DailyTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
