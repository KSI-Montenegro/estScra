# Daily contacts and cumulative listing feed

The daily workflow now collects contacts after listing capture and before photo processing.
It updates the source JSON/CSV/ZIP and resulting photo-enriched listings, then publishes a
cumulative listing feed with the newest ready photo selections after photo processing.

Read this public JSON index and follow its assets links:

https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/listings/latest.json

No API credentials are needed for these public GET requests. Assets include listings.json,
listings.csv, listings-with-contacts.zip, summary.json, phone_updates.json and contact_queue.csv.
This cumulative feed starts with the 164 listings exported on 2–3 November 2026 and adds new
daily captures. It does not replace the separate original 1,797-property import under
listing_details/. Upsert by property_key. The existing latest daily photo feed is also refreshed
with contact updates; historical immutable reports and old GitHub artifacts are retained as captured.
The cumulative feed joins the current photo catalog, selecting only ready_for_listing images.
contact_backfill.json is the original recovery input; use the public index for current contacts.
See contact_pipeline_validation.json for the verified backfill counts and live workflow runs.

## Collection

- All sources: preserve existing contacts; normalize phones in the extracted description of
  the exact listing to international form. Montenegro local numbers are normalized using +382.
- Realitica: read the phone-button handler belonging to the current listing's advertiser panel.
  Both simple digit strings and formatted/multiple-number variants are supported. No JS execution.
- Estitor: use its listing-specific phone reveal endpoint. No call is initiated. Preserve each
  response immediately; stop at exhausted hourly/daily counters or an explicit quota response.
- Patuljak: read public description numbers and seller tel links. A login-only panel is marked
  login_required. An existing owner-supplied PATULJAK_COOKIE can enable authenticated reads.
  Set it in the local secret environment and GitHub Actions secrets; do not put it in Git or chat.
  The cookie is sent only to the exact Patuljak host and redirects are disabled for that request.
  A same-host redirect to login is reported as login_required; a redirect to the site root
  is unavailable. Other HTTP/access failures remain retryable with their actual HTTP code.
  Legacy generic authenticated HTTP errors are rechecked once to classify them correctly.
- Footers, support numbers, unrelated ads and popups are excluded. The known Estitor platform
  number is excluded. No contact forms, messages, callbacks or outbound calls are submitted.

Contact JSON includes contact_phones, phone_e164, contact_candidates with source and listing_url,
phone_collection_status, phone_checked_at and phone_error_code. Multiple numbers are retained
without selecting a primary. Advertiser phone collection does not establish private-owner identity
or permission/readiness to publish or contact. The platform's existing preparation rules still apply.

Statuses are found, multiple_numbers, login_required, no_public_number, unavailable, pending,
quota_deferred and retry. Missing numbers stay blank. Authentication/configuration and unexpected
code failures fail the contact step; login walls, quotas and unavailable listings remain visible
in the queue without falsely reporting a successful contact lookup.

## Resumption and limits

contact-state.json on the daily-scraper-state branch stores cumulative records, results and shared
Estitor cooldowns. Successful results are reused, and failed/pending work resumes automatically.
An interrupted reveal or ambiguous POST failure is not immediately replayed. Reported zero hourly
or daily counters cause conservative one-hour or 24-hour waits; an unclassified quota response
causes a 24-hour wait. These are cooldowns, not claims about an exact server reset schedule.
Bright Data is a fallback for blocked access, not a way to rotate around a reveal quota.

Defaults: 300 pending listings checked, 10 reveal attempts, 20 Bright Data page/POST attempts
shared within this contact stage, and a 600-second cooperative processing budget. These are
separate from the scraper/photo request caps. A local session lock and workflow concurrency guard
prevent overlapping state writes. Source statuses and individual failures remain in exported reports.

Manual Run workflow has a contacts_only option to update pending contacts and publish feeds
without new listing discovery, model installation or photo editing. max_phone_listings sets the
pending-listing cap. The normal daily schedule includes contacts automatically. A lightweight
contacts-only run also executes hourly at minute 37 to resume queued lookups after cooldowns;
it skips listing discovery, model installation and all image processing. Deferred lookups
do not consume the actual-listing check limit or displace another source's accessible ads.
Unchanged contact exports use deterministic ZIPs so idle retries reuse immutable assets.

Local collection: python daily_scraper/contact_enrichment.py --collect

Optional automatic renewal of the owner's Patuljak login uses a private TextBee
SMS receiver. See [Patuljak SMS authentication](PATULJAK_SMS_AUTH.md) for pairing,
Actions configuration, session storage, and renewal limits. This is separate from
advertiser phone numbers and does not request SMS for individual login-walled ads.

Local publication: env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE python daily_scraper/contact_enrichment.py --publish

The existing R2 secrets and endpoint config are reused. Versioned outputs are immutable; latest
pointers use conditional writes. Binary photos and their ready/held selections are not modified
by contact backfilling.
