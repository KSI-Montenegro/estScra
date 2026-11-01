"""Bright Data Browser API transport. Credentials remain in process memory."""
import base64
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


class BrowserFailure(Exception):
    pass


class BrowserAPI:
    def __init__(self, key, zone, allowed_hosts, max_body=8_000_000):
        self.key, self.zone = key, zone
        self.allowed_hosts, self.max_body = allowed_hosts, max_body
        self.credentials = None
        self.unavailable = False

    def account_get(self, path, params=None):
        url = 'https://api.brightdata.com' + path
        if params:
            url += '?' + urlencode(params)
        req = Request(url, headers={'Authorization': 'Bearer ' + self.key})
        try:
            with urlopen(req, timeout=25) as response:
                return json.loads(response.read(1_000_000))
        except HTTPError as exc:
            raise BrowserFailure('browser_account_http_' + str(exc.code)) from None
        except (ValueError, URLError, OSError):
            raise BrowserFailure('browser_account_lookup_failed') from None

    def auth_headers(self):
        if self.credentials is None:
            account = self.account_get('/status')
            config = self.account_get('/zone', {'zone': self.zone})
            if not isinstance(account, dict) or not isinstance(config, dict):
                raise BrowserFailure('browser_account_response_invalid')
            if config.get('plan', {}).get('type') != 'browser_api':
                raise BrowserFailure('configured_zone_is_not_browser_api')
            customer, passwords = account.get('customer'), config.get('password')
            if not isinstance(customer, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', customer):
                raise BrowserFailure('browser_customer_id_missing')
            if not re.fullmatch(r'[A-Za-z0-9_-]+', self.zone):
                raise BrowserFailure('browser_zone_invalid')
            if not isinstance(passwords, list) or not passwords or not isinstance(passwords[0], str):
                raise BrowserFailure('browser_zone_password_missing')
            username = 'brd-customer-' + customer + '-zone-' + self.zone
            self.credentials = base64.b64encode((username + ':' + passwords[0]).encode()).decode()
        return {'Authorization': 'Basic ' + self.credentials}

    def get(self, url):
        if urlsplit(url).scheme != 'https' or urlsplit(url).hostname not in self.allowed_hosts:
            raise BrowserFailure('browser_unsupported_url')
        if self.unavailable:
            raise BrowserFailure('brightdata_browser_unavailable')
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserFailure('install_playwright_for_browser_api') from None
        headers = self.auth_headers()
        try:
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(
                    'wss://brd.superproxy.io:9222', headers=headers, timeout=20_000)
                try:
                    page = browser.new_page()
                    # Save photo/font transfer; challenge resources on other hosts
                    # are left to the provider's browser service.
                    page.route('**/*', lambda route: route.abort()
                               if route.request.resource_type in ('image', 'media', 'font')
                               and urlsplit(route.request.url).hostname == urlsplit(url).hostname
                               else route.continue_())
                    response = page.goto(url, wait_until='domcontentloaded', timeout=60_000)
                    deadline = time.monotonic() + 30
                    while re.search(r'just a moment|checking your browser', page.title(), re.I):
                        if time.monotonic() >= deadline:
                            raise BrowserFailure('browser_challenge_not_resolved')
                        page.wait_for_timeout(1000)
                    if response and response.status == 429:
                        raise BrowserFailure('browser_source_http_429')
                    # A provider challenge can reload the page; accept only an
                    # actual listing/search DOM, validated by the caller as well.
                    final = page.url
                    if urlsplit(final).scheme != 'https' or urlsplit(final).hostname not in self.allowed_hosts:
                        raise BrowserFailure('browser_redirect_host_invalid')
                    body = page.content().encode()
                    if len(body) > self.max_body:
                        raise BrowserFailure('browser_response_too_large')
                    if response and response.status >= 400:
                        if response.status != 403 or not page.locator('h1, #listing_body, a[href*="/id-"]').count():
                            raise BrowserFailure('browser_source_http_' + str(response.status))
                    return body, final, 'brightdata_browser_api'
                finally:
                    browser.close()
        except BrowserFailure:
            raise
        except Exception as exc:
            # Playwright errors can contain auth headers and CDP endpoints. Only
            # fixed classifications leave this transport, never the raw error.
            message = str(exc)
            if 'ECONNREFUSED' in message:
                self.unavailable = True
                raise BrowserFailure('browser_network_port_refused') from None
            if any(code in message for code in ('401 Unauthorized', '407', 'auth_failed')):
                self.unavailable = True
                raise BrowserFailure('browser_authentication_failed') from None
            raise BrowserFailure('browser_connection_or_navigation_failed') from None
