"""Patuljak session reuse and bounded, TextBee-assisted login for the owner's account.

No SMS is requested unless the portal explicitly shows a logged-out navigation,
automatic login is enabled, and the signed TextBee device webhook has been paired.
Cookies and codes never enter scraper checkpoints or exports.
"""
import base64
import hashlib
from http.cookies import SimpleCookie
from http.cookiejar import CookieJar
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, HTTPCookieProcessor
import uuid

from lxml import html, etree

HOST = 'patuljak.me'
AUTH_NAMES = ('PHPSESSID', '5tg', 'id')


class AuthFailure(Exception):
    """Only fixed classifications, never response bodies, phone numbers or codes."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def filtered_cookie(value):
    if not isinstance(value, str) or len(value) > 8192 or '\r' in value or '\n' in value:
        raise AuthFailure('invalid_session_cookie')
    jar = SimpleCookie()
    try:
        jar.load(value)
    except Exception:
        raise AuthFailure('invalid_session_cookie') from None
    parts = []
    for name in AUTH_NAMES:
        if name in jar:
            token = jar[name].value
            if not re.fullmatch(r'[A-Za-z0-9_.=+/-]+', token):
                raise AuthFailure('invalid_session_cookie')
            parts.append(name + '=' + token)
    return '; '.join(parts)


def account_phone(explicit, cookie):
    value = explicit.strip() if isinstance(explicit, str) else ''
    if not value:
        jar = SimpleCookie(); jar.load(cookie)
        try:
            value = base64.b64decode(jar['5tg'].value, validate=True).decode('ascii')
        except (KeyError, ValueError, UnicodeError):
            raise AuthFailure('login_phone_configuration_required') from None
    if not re.fullmatch(r'\+382[2-6]\d{7}', value):
        raise AuthFailure('invalid_login_phone')
    return value


class Broker:
    def __init__(self, base, key):
        parsed = urlsplit(base)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ('', '/') or len(key) < 32):
            raise AuthFailure('invalid_broker_configuration')
        self.base, self.key = base.rstrip('/'), key
        self.client = build_opener(NoRedirect())

    def request(self, path, method='GET', data=None):
        if not re.fullmatch(r'/v1/(?:status|session|login/begin|login/[a-f0-9-]{36}/(?:code|complete|fail))', path):
            raise AuthFailure('invalid_broker_path')
        body = json.dumps(data).encode() if data is not None else None
        req = Request(self.base + path, method=method, data=body,
                      headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                               'User-Agent': 'GRADEN-Patuljak-Auth/1.0'})
        try:
            with self.client.open(req, timeout=20) as response:
                raw = response.read(65_537)
            if len(raw) > 65_536:
                raise AuthFailure('broker_response_limit')
            return json.loads(raw)
        except HTTPError as exc:
            known = {'textbee_pairing_required', 'login_already_pending', 'login_cooldown', 'account_mismatch',
                     'login_expired_or_missing', 'code_already_claimed'}
            try:
                code = json.loads(exc.read(4096)).get('error')
            except (ValueError, AttributeError):
                code = None
            raise AuthFailure('broker_' + code if code in known else 'broker_http_' + str(exc.code)) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise AuthFailure('broker_connection_or_response_failed') from None


def safe_portal_url(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.hostname == HOST and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and not parsed.fragment
                and not parsed.path.startswith('/logout')
                and (not parsed.path.startswith('/process/') or bool(re.search(r'login|logov|prijav|kod|verif|aktiv', parsed.path, re.I))))
    except ValueError:
        return False


def form_data(form):
    fields = {}
    for node in form.xpath('.//input[@name]'):
        if node.get('type', 'text').lower() == 'hidden':
            name, value = node.get('name'), node.get('value', '')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name) or len(value) > 2048 or len(fields) >= 32:
                raise AuthFailure('unsupported_login_form')
            fields[name] = value
    buttons = form.xpath('.//button[not(@type) or @type="submit"] | .//input[@type="submit"]')
    if buttons and buttons[0].get('name'):
        name = buttons[0].get('name')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise AuthFailure('unsupported_login_form')
        fields[name] = buttons[0].get('value', '')
    return fields


def verification_form(body, page_url):
    try:
        root = html.fromstring(body)
    except (ValueError, etree.ParserError):
        raise AuthFailure('verification_form_unrecognized') from None
    candidates = []
    for form in root.xpath('//form'):
        action = urljoin(page_url, form.get('action') or page_url)
        if not safe_portal_url(action) or not re.search(r'login|logov|prijav|kod|verif|aktiv', urlsplit(action).path, re.I):
            continue
        code_fields = [node for node in form.xpath('.//input[@name]')
                       if node.get('type', 'text').lower() in ('text', 'tel', 'number', 'password') and
                       (node.get('autocomplete') == 'one-time-code' or re.fullmatch(
                           r'(?:code|kod|pin|otp|login_code|verification_code|sms_code|token|activation_code)', node.get('name'), re.I))]
        code_target = None
        if len(code_fields) == 1:
            code_target = code_fields[0].get('name')
        elif 4 <= len(code_fields) <= 8:
            names = [node.get('name') for node in code_fields]
            if names == ['phone_code_' + str(i) for i in range(1, len(names) + 1)]:
                code_target = tuple(names)
        if code_target:
            candidates.append((action, form_data(form), code_target, form.get('enctype', '')))
    if len(candidates) != 1:
        raise AuthFailure('verification_form_unrecognized')
    return candidates[0]


def fill_verification_code(fields, target, code):
    if not isinstance(code, str) or not re.fullmatch(r'\d{4,8}', code):
        raise AuthFailure('invalid_sms_code')
    if isinstance(target, tuple):
        if len(target) != len(code):
            raise AuthFailure('verification_code_length_mismatch')
        fields.update(zip(target, code))
    else:
        fields[target] = code


class PatuljakSite:
    def __init__(self):
        self.cookies = CookieJar()
        self.client = build_opener(NoRedirect(), HTTPCookieProcessor(self.cookies))

    def session_valid(self, cookie):
        headers = {'User-Agent': 'Mozilla/5.0'}
        if cookie:
            headers['Cookie'] = filtered_cookie(cookie)
        try:
            with build_opener(NoRedirect()).open(Request('https://' + HOST + '/', headers=headers), timeout=25) as response:
                if response.status != 200:
                    raise AuthFailure('patuljak_session_probe_blocked')
                body = response.read(2_000_001)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise AuthFailure('patuljak_session_probe_blocked') from None
        if len(body) > 2_000_000:
            raise AuthFailure('patuljak_session_probe_limit')
        try:
            root = html.fromstring(body)
            paths = [urlsplit(urljoin('https://' + HOST + '/', value)).path for value in root.xpath('//a/@href')]
        except (ValueError, etree.ParserError):
            raise AuthFailure('patuljak_session_probe_unrecognized') from None
        if '/logout_k' in paths and any(path.startswith('/mojprofil/') for path in paths):
            return True
        if '/login.html' in paths:
            return False
        raise AuthFailure('patuljak_session_probe_unrecognized')

    def request(self, url, fields=None, enctype=''):
        for redirect in range(3):
            if not safe_portal_url(url):
                raise AuthFailure('login_target_not_allowed')
            headers = {'User-Agent': 'Mozilla/5.0', 'Origin': 'https://' + HOST, 'Referer': 'https://' + HOST + '/login.html'}
            body = None
            if fields is not None:
                if 'multipart/form-data' in enctype:
                    boundary = 'graden-' + uuid.uuid4().hex
                    parts = []
                    for name, value in fields.items():
                        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
                            raise AuthFailure('unsupported_login_form')
                        parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n' + str(value) + '\r\n').encode())
                    body = b''.join(parts) + ('--' + boundary + '--\r\n').encode()
                    headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
                else:
                    body = urlencode(fields).encode()
                    headers['Content-Type'] = 'application/x-www-form-urlencoded'
            try:
                with self.client.open(Request(url, data=body, headers=headers), timeout=30) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise AuthFailure('login_response_limit')
                return raw, url
            except HTTPError as exc:
                if exc.code in (301, 302, 303) and redirect < 2:
                    url = urljoin(url, (exc.headers or {}).get('Location', ''))
                    fields = None  # Follow only as GET; never replay an SMS-request POST.
                    continue
                raise AuthFailure('login_http_' + str(exc.code)) from None
            except (URLError, TimeoutError, OSError):
                raise AuthFailure('login_request_outcome_unknown') from None
        raise AuthFailure('login_redirect_limit')

    def phone_form(self, phone):
        body, page = self.request('https://' + HOST + '/login.html')
        try:
            root = html.fromstring(body)
        except (ValueError, etree.ParserError):
            raise AuthFailure('phone_form_unrecognized') from None
        forms = root.xpath('//form[.//input[@name="phone"] and .//select[@name="code"]]')
        if len(forms) != 1:
            raise AuthFailure('phone_form_unrecognized')
        form = forms[0]; action = urljoin(page, form.get('action', ''))
        if not safe_portal_url(action) or urlsplit(action).path != '/login_code.html' or '+382' not in form.xpath('.//select[@name="code"]/option/@value'):
            raise AuthFailure('phone_form_unrecognized')
        fields = form_data(form); fields.update(code='+382', phone=phone[4:])
        return action, fields, form.get('enctype', '')

    def session_header(self):
        return filtered_cookie('; '.join(c.name + '=' + c.value for c in self.cookies if c.name in AUTH_NAMES and
                                        c.domain.lstrip('.') == HOST and c.path == '/'))


def resolve_cookie(env, broker=None, site=None):
    fallback = filtered_cookie(env.get('PATULJAK_COOKIE', ''))
    base, key = env.get('PATULJAK_AUTH_BROKER_URL', ''), env.get('PATULJAK_AUTH_CLIENT_KEY', '')
    if not base or not key:
        return fallback, {'status': 'static_session'}
    cookie, request_id, phone_hash = fallback, None, None
    try:
        broker = broker or Broker(base, key)
        stored = broker.request('/v1/session')
        if stored.get('cookie'):
            cookie = filtered_cookie(stored['cookie'])
        site = site or PatuljakSite()
        try:
            phone = account_phone(env.get('PATULJAK_LOGIN_PHONE', ''), fallback or cookie)
            phone_hash = hashlib.sha256(phone.encode()).hexdigest()
        except AuthFailure:
            phone = None
        if stored.get('phone_hash') and phone_hash and stored['phone_hash'] != phone_hash:
            raise AuthFailure('broker_account_mismatch')
        active = site.session_valid(cookie)
        if not active and fallback and cookie != fallback and site.session_valid(fallback):
            cookie, active = fallback, True
        if active:
            if phone_hash and cookie != stored.get('cookie'):
                broker.request('/v1/session', 'PUT', {'phone_hash': phone_hash, 'cookie': cookie})
            return cookie, {'status': 'session_active', 'source': 'broker' if cookie == stored.get('cookie') else 'github_secret'}
        if env.get('PATULJAK_AUTO_LOGIN', '').lower() not in ('1', 'true', 'yes'):
            return cookie, {'status': 'session_expired_auto_login_disabled'}
        if not phone:
            raise AuthFailure('login_phone_configuration_required')
        status = broker.request('/v1/status')
        if not status.get('configured') or not status.get('webhook_verified'):
            return cookie, {'status': 'textbee_pairing_required'}
        action, fields, enctype = site.phone_form(phone)
        pending = broker.request('/v1/login/begin', 'POST', {'phone_hash': phone_hash})
        request_id = pending.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9-]{36}', request_id):
            request_id = None
            raise AuthFailure('broker_invalid_pending_response')
        body, page = site.request(action, fields, enctype)  # One SMS-request submission.
        action, fields, code_field, enctype = verification_form(body, page)
        try:
            wait = min(240, max(30, int(env.get('PATULJAK_SMS_WAIT_SECONDS', '180'))))
        except (TypeError, ValueError):
            raise AuthFailure('invalid_sms_wait_configuration') from None
        deadline = time.monotonic() + wait
        code = None
        while time.monotonic() < deadline:
            result = broker.request('/v1/login/' + request_id + '/code', 'POST', {'phone_hash': phone_hash})
            if result.get('code'):
                code = result['code']; break
            time.sleep(3)
        if not isinstance(code, str) or not re.fullmatch(r'\d{4,8}', code):
            raise AuthFailure('sms_code_timeout')
        fill_verification_code(fields, code_field, code)
        site.request(action, fields, enctype)
        fresh = site.session_header()
        if not fresh or not site.session_valid(fresh):
            raise AuthFailure('sms_login_not_verified')
        broker.request('/v1/login/' + request_id + '/complete', 'POST', {'phone_hash': phone_hash, 'cookie': fresh})
        return fresh, {'status': 'session_renewed', 'source': 'textbee'}
    except AuthFailure as exc:
        if request_id and broker:
            try:
                broker.request('/v1/login/' + request_id + '/fail', 'POST', {'phone_hash': phone_hash})
            except AuthFailure:
                pass
        return fallback, {'status': 'authentication_deferred', 'reason': str(exc)}
