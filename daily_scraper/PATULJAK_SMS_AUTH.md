# Patuljak login renewal with TextBee

The contact collector can renew the account owner's expired Patuljak session with
an SMS received on their Android phone. It reuses a valid session first. Individual
ads that redirect to login do not prove that the account session has expired.

## Current deployment

- Repository: `KSI-Montenegro/estScra`, daily property workflow, contact collection step.
- Private broker: `https://graden-patuljak-auth.automobili-d2153dc7.workers.dev`.
- TextBee device: `6af6051e3d2c9203edfe3ae7`, hosted TextBee.
- Webhook: `POST /webhooks/textbee`, event `MESSAGE_RECEIVED`.
- Automatic login is **enabled** (`PATULJAK_AUTO_LOGIN=true`) following live validation.
- On 2026-11-13, a real incoming device SMS verified webhook delivery. One actual
  Patuljak login then requested one SMS, automatically claimed its code, submitted
  the six separate code fields, and verified/cached the fresh account session.

In the TextBee dashboard, create a webhook using that URL, `MESSAGE_RECEIVED`, and
the signing secret in the locally generated `private/textbee-webhook-setup.json`.
That file is private and excluded from Git. Receive an ordinary SMS on the
configured device to establish that signed webhook delivery works. No TextBee API
key is needed by the scraper. Do not put SMS codes or signing secrets in chat.

## Daily flow and storage

1. When Patuljak contacts need collection, fetch the private cached session.
2. Probe Patuljak's homepage navigation. Authenticated profile/logout links mean
   reuse it. Public login navigation means expired. A blocked or unrecognized
   response defers renewal without sending SMS.
3. If renewal is enabled and the TextBee device has delivered a genuine signed
   event, reserve one pending attempt and submit the account phone once.
4. TextBee signs the exact webhook body with its timestamped
   `X-Textbee-Signature: t=...,v1=...` HMAC-SHA256 scheme. The receiver checks
   signature, timestamp, device, sender/message, original SMS time, and replay ID.
5. The collector polls for one code for up to 180 seconds, submits it once, and
   checks the new session before caching it. Ambiguous request failures are not
   retried automatically. Unsupported form changes defer the attempt.
6. Phone collection, listing exports, and existing photo processing then continue
   through their existing paths. Missing contacts retain their retry/login status.

Cookies and short-lived codes live in a **private Cloudflare Durable Object**
bound to `graden-patuljak-auth`, not in the public photo bucket. Codes expire after
five minutes and are claimed once. Sessions are cached for seven days and checked
against the portal before use. There is one active challenge, a fifteen-minute
cooldown, and at most three login attempts per rolling day. Only fixed status and
error classifications go into contact state, summaries, and public exports.

The account's login phone is separate from advertiser contacts. An explicit
`PATULJAK_LOGIN_PHONE` overrides the phone derived privately from the existing
account cookie. Current login support is for a Montenegro `+382` account.

## Configuration and operator access

GitHub Actions secrets:

- `PATULJAK_AUTH_CLIENT_KEY`: private broker API bearer key.
- `PATULJAK_COOKIE`: existing session fallback/bootstrap.
- `PATULJAK_LOGIN_PHONE`: optional explicit account phone.

GitHub Actions variables:

- `PATULJAK_AUTH_BROKER_URL`: deployed broker base URL.
- `PATULJAK_AUTO_LOGIN`: keep `false` until webhook pairing and a real OTP test
  succeed; then set `true` to allow future renewals.

`GET /health` is public. All `/v1/` routes require the client bearer key:
`GET /v1/status` gives pairing/session/pending metadata;
`GET /v1/session` returns the private cookie and must never be logged;
`PUT /v1/session` can seed a portal-verified session with its account SHA256 phone
hash. Login begin, code claim, completion and failure routes are implemented by
`patuljak_auth.py`. Do not call login-begin just to check health: it consumes an
attempt. The Worker has no GitHub PAT, Cloudflare admin token, or public R2 write
credentials at runtime.

To deploy or update on the same Cloudflare account:

```sh
python daily_scraper/provision_auth_broker.py \
  --token-file /secure/path/cloudflare-token \
  --account YOUR_ACCOUNT_ID \
  --device-id YOUR_TEXTBEE_DEVICE_ID \
  --credentials-file private/patuljak-auth.json
```

Retain that private credentials file securely: updates refuse to overwrite an
existing broker without it, preventing accidental key rotation. The script writes
both local private JSON files with mode 0600 and checks Worker ownership. Changing
the device requires a new signed message from the new device. A repository transfer
does not change the receiver URL; verify the new repository still has its Actions
secrets and variables before enabling the workflow.

Run Python tests with `python -m unittest discover -s daily_scraper -p 'test_*.py'`
and receiver tests with `node --test daily_scraper/auth_broker/worker.test.mjs`
(Node 22+). Logs and test fixtures must use synthetic cookies, codes, and phones.
