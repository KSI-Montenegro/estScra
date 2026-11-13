import base64
import io
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import patuljak_auth as auth

PHONE = '+38269123456'
OLD = 'PHPSESSID=old-session; 5tg=' + base64.b64encode(PHONE.encode()).decode() + '; id=123'
NEW = OLD.replace('old-session', 'new-session')
PENDING = '11111111-2222-3333-4444-555555555555'
CODE_FORM = b'<form method="POST" action="/process/login"><input type="hidden" name="csrf" value="fixture"><input name="kod" autocomplete="one-time-code"><button name="verify" value="1">Confirm</button></form>'
SPLIT_CODE_FORM = ('<form method="POST" action="/login_code.html">'
                   '<input type="hidden" name="phone_2" value="fixture-account">'
                   + ''.join('<input type="tel" name="phone_code_'+str(i)+'" autocomplete="one-time-code" maxlength="1">' for i in range(1,7))
                   + '<input type="hidden" name="submit_code" value="submit_code" autocomplete="one-time-code">'
                   '<button name="login" value="confirm">Confirm</button></form>').encode()


def env(**extra):
    return {'PATULJAK_COOKIE': OLD, 'PATULJAK_AUTH_BROKER_URL':'https://broker.example.test',
            'PATULJAK_AUTH_CLIENT_KEY':'k'*40, 'PATULJAK_AUTO_LOGIN':'true', **extra}


class FakeBroker:
    def __init__(self, stored=None, paired=True): self.calls=[]; self.stored=stored or {}; self.paired=paired
    def request(self,path,method='GET',data=None):
        self.calls.append((path,method,data))
        if path=='/v1/session': return self.stored if method=='GET' else {'stored':True}
        if path=='/v1/status': return {'configured':self.paired,'webhook_verified':self.paired}
        if path.endswith('/begin'):return {'request_id':PENDING}
        if path.endswith('/code'):return {'code':'123456'}
        return {'completed':True}


class FakeSite:
    def __init__(self, valid=False): self.valid=valid;self.posts=[];self.probes=[]
    def session_valid(self,cookie): self.probes.append(cookie);return self.valid or cookie==NEW
    def phone_form(self,phone): return 'https://patuljak.me/login_code.html',{'phone':phone[4:],'code':'+382'},'multipart/form-data'
    def request(self,url,fields=None,enctype=''):
        self.posts.append((url,fields,enctype));return CODE_FORM,url
    def session_header(self): return NEW


class AuthTests(unittest.TestCase):
    def test_existing_static_workflow_never_sends_an_sms(self):
        with patch.object(auth,'Broker') as broker,patch.object(auth,'PatuljakSite') as site:
            cookie,status=auth.resolve_cookie({'PATULJAK_COOKIE':OLD})
        self.assertEqual(cookie,OLD);self.assertEqual(status['status'],'static_session')
        broker.assert_not_called();site.assert_not_called()

    def test_valid_session_is_reused_even_when_individual_ads_require_login(self):
        b,s=FakeBroker(),FakeSite(valid=True)
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,OLD);self.assertEqual(status['status'],'session_active')
        self.assertFalse(any(path.endswith('/begin') for path,_,_ in b.calls));self.assertEqual(s.posts,[])

    def test_expired_session_waits_for_pairing_before_requesting_sms(self):
        b,s=FakeBroker(paired=False),FakeSite()
        _,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(status['status'],'textbee_pairing_required');self.assertFalse(s.posts)
        self.assertFalse(any(path.endswith('/begin') for path,_,_ in b.calls))

    def test_global_probe_block_is_not_treated_as_expired_authentication(self):
        b,s=FakeBroker(),FakeSite();s.session_valid=MagicMock(side_effect=auth.AuthFailure('patuljak_session_probe_blocked'))
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,OLD);self.assertEqual(status['reason'],'patuljak_session_probe_blocked');self.assertFalse(s.posts)

    def test_authenticated_login_posts_phone_once_then_code_and_caches_session(self):
        b,s=FakeBroker(),FakeSite()
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,NEW);self.assertEqual(status['status'],'session_renewed')
        self.assertEqual(len(s.posts),2);self.assertEqual(s.posts[0][1]['phone'],PHONE[4:])
        self.assertEqual(s.posts[1][1]['kod'],'123456')
        self.assertTrue(any(path.endswith('/complete') and data['cookie']==NEW for path,_,data in b.calls))
        self.assertNotIn(PHONE,str(status));self.assertNotIn('123456',str(status));self.assertNotIn(NEW,str(status))

    def test_live_portal_split_code_form_submits_digits_with_hidden_fields(self):
        b,s=FakeBroker(),FakeSite()
        def request(url,fields=None,enctype=''):
            s.posts.append((url,dict(fields)));return SPLIT_CODE_FORM,url
        s.request=request
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,NEW);self.assertEqual(status['status'],'session_renewed')
        self.assertEqual(len(s.posts),2)
        sent=s.posts[1][1]
        self.assertEqual([sent['phone_code_'+str(i)] for i in range(1,7)],list('123456'))
        self.assertEqual(sent['submit_code'],'submit_code')
        self.assertEqual(sent['phone_2'],'fixture-account')
        self.assertEqual(sent['login'],'confirm')

    def test_split_code_length_or_missing_digit_never_submits_a_partial_code(self):
        b,s=FakeBroker(),FakeSite()
        s.request=MagicMock(return_value=(SPLIT_CODE_FORM,'https://patuljak.me/login_code.html'))
        original=b.request
        def request(path,method='GET',data=None):
            return {'code':'1234'} if path.endswith('/code') else original(path,method,data)
        b.request=request
        _,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(status['reason'],'verification_code_length_mismatch')
        self.assertEqual(s.request.call_count,1)
        with self.assertRaises(auth.AuthFailure):
            auth.verification_form(SPLIT_CODE_FORM.replace(b'phone_code_3',b'unrelated_field'),'https://patuljak.me/login_code.html')

    def test_unknown_verification_form_stops_without_repeating_sms(self):
        b,s=FakeBroker(),FakeSite()
        def request(url,fields=None,enctype=''):
            s.posts.append((url,fields));return b'<html>unrecognized challenge</html>',url
        s.request=request
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,OLD);self.assertEqual(status['reason'],'verification_form_unrecognized')
        self.assertEqual(len(s.posts),1);self.assertTrue(any(path.endswith('/fail') for path,_,_ in b.calls))

    def test_wrong_broker_account_never_reaches_patulkjak_or_uses_its_cookie(self):
        b=FakeBroker({'cookie':NEW,'phone_hash':'b'*64});s=FakeSite()
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,OLD);self.assertEqual(status['reason'],'broker_account_mismatch');self.assertFalse(s.probes)

    def test_manual_cookie_refresh_can_replace_expired_broker_session(self):
        b=FakeBroker({'cookie':OLD,'phone_hash':__import__('hashlib').sha256(PHONE.encode()).hexdigest()});s=FakeSite()
        cookie,status=auth.resolve_cookie(env(PATULJAK_COOKIE=NEW),b,s)
        self.assertEqual(cookie,NEW);self.assertEqual(status['source'],'github_secret');self.assertFalse(s.posts)

    def test_untrusted_forms_and_redirect_targets_are_rejected(self):
        for action in ['https://example.org/process/login','https://patuljak.me/process/posaljiPoruku','https://patuljak.me/logout_k']:
            with self.subTest(action=action):
                body=('<form action="'+action+'"><input name="code" autocomplete="one-time-code"></form>').encode()
                with self.assertRaises(auth.AuthFailure):auth.verification_form(body,'https://patuljak.me/login_code.html')

    def test_empty_otp_response_closes_pending_attempt_without_repeating_sms(self):
        b,s=FakeBroker(),FakeSite()
        s.request=MagicMock(return_value=(b'', 'https://patuljak.me/login_code.html'))
        cookie,status=auth.resolve_cookie(env(),b,s)
        self.assertEqual(cookie,OLD)
        self.assertEqual(status['reason'],'verification_form_unrecognized')
        self.assertEqual(s.request.call_count,1)
        self.assertTrue(any(path.endswith('/fail') for path,_,_ in b.calls))

    def test_cookie_filter_excludes_tracking_values_and_injection(self):
        self.assertEqual(auth.filtered_cookie(OLD+'; _ga=tracking'),OLD)
        with self.assertRaises(auth.AuthFailure):auth.filtered_cookie(OLD+'\r\nExtra: value')
        self.assertEqual(auth.account_phone('',OLD),PHONE)

    def test_home_probe_requires_real_authenticated_navigation(self):
        site=auth.PatuljakSite()
        for body,expected in [(b'<a href="/logout_k">out</a><a href="/mojprofil/mojioglasi">profile</a>',True),
                              (b'<a href="/login.html">login</a>',False)]:
            response=MagicMock(status=200);response.__enter__.return_value=response;response.read.return_value=body
            opener=MagicMock();opener.open.return_value=response
            with patch.object(auth,'build_opener',return_value=opener):self.assertEqual(site.session_valid(OLD),expected)
        response=MagicMock(status=202);response.__enter__.return_value=response
        opener=MagicMock();opener.open.return_value=response
        with patch.object(auth,'build_opener',return_value=opener):
            with self.assertRaisesRegex(auth.AuthFailure,'probe_blocked'):site.session_valid(OLD)

    def test_post_transport_failure_is_never_replayed(self):
        site=auth.PatuljakSite();site.client=MagicMock()
        site.client.open.side_effect=TimeoutError('no response')
        with self.assertRaisesRegex(auth.AuthFailure,'outcome_unknown'):
            site.request('https://patuljak.me/login_code.html',{'phone':'69123456'})
        self.assertEqual(site.client.open.call_count,1)


if __name__=='__main__':unittest.main()
