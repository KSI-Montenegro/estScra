# GRADEN daily property feed

Discovers ads from Realitica, Patuljak and Estitor without a browser extension.
Uses direct verified HTTPS first, with the existing Bright Data zone as a fallback.
`--mode always` routes every page through Bright Data; this uses more paid requests.
If the HTTP proxy returns target HTTP 403, the configured Browser API zone is tried.
The default browser fallback uses the account's existing `estitorscraper2` zone.
No browser login, phone reveal requests, paid image editing or automatic publishing of listings.

## Run locally

Python 3.10+ on Linux/macOS; Windows can use WSL. Install dependencies in a virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python daily_scraper.py --api-key-file /secure/path/brightdata_api_key
```

These commands assume the portable ZIP is extracted. In the repository use
`python daily_scraper/daily_scraper.py` and `daily_scraper/requirements.txt` instead.
Alternatively set `BRIGHTDATA_API_KEY` using your secret manager. Never put the key in Git.
Optional `BRIGHTDATA_ZONE` defaults to the previously working `esstitor_site_phone_scraper`.
Optional `BRIGHTDATA_BROWSER_ZONE` defaults to `estitorscraper2`. The Browser API customer ID
and zone password are retrieved at runtime through the API key using `GET /status` and
`GET /zone`. Browser credentials stay in memory and never enter exports, state, or logs.
Use `--browser-zone none` to disable browser fallback.

To use the hosted Browser API for every page:

```bash
.venv/bin/python daily_scraper.py --mode browser --browser-zone estitorscraper2 --api-key-file /secure/path/brightdata_api_key --max-api-requests 10 --max-pages 1 --max-listings 1
```

No local browser binary or extension is needed. Playwright connects to the remote browser over
verified WSS on `brd.superproxy.io:9222`; that port must be reachable from the execution host.
Each page gets a bounded session that closes immediately afterward. Temporary browser
connection/navigation failures receive one retry, counted against the same page-request cap;
HTTP 429, authentication and refused-port failures are not retried. Once a browser route succeeds
for a source, the rest of that run uses it directly to avoid repeated blocked proxy requests.
First-party image, font
and media requests are blocked to reduce transfer. HTTP and browser page attempts share the
request cap; account metadata lookups are separate control requests. This limits page attempts,
not monetary spend. Existing account pricing applies; no new zone or subscription was created.

For a small initial trial:

```bash
.venv/bin/python daily_scraper.py --max-pages 1 --max-listings 9 --max-api-requests 10
```

Defaults: 10 pages per search, up to 300 new captures, 100 Bright Data requests;
at least 0.7 seconds between request starts per host. HTTP 429 pauses that source.
The API-request cap limits call count, not monetary spend; pricing belongs to Bright Data.
`--mode direct` never uses the paid fallback. TLS verification always stays enabled.

Live validation on 2026-11-01: direct discovery and captures worked on all three sources.
The existing datacenter Bright Data zone accepted API requests but returned proxied target
HTTP 403 for the tested Estitor and Realitica search pages. Its fallback transport is implemented,
but that zone is not a verified unblocker for these pages. Auto mode uses the working direct
route first and records provider/target failures separately. A compatible working zone may be
configured later through `BRIGHTDATA_ZONE`; browser_api zones use the separate Playwright
transport included in this version, rather than the HTTP `/request` endpoint.

Additional live tests on 2026-11-01 confirmed a full Patuljak discovery and listing capture
through the Bright Data HTTP API (2 requests, no errors). This cloud workspace refuses port
9222, but a GitHub-hosted run using the owner-configured API secret successfully captured one
listing from each source, including Estitor through the existing `estitorscraper2` Browser API
zone. Two rental search navigations failed in that first run; bounded retry support was added.
The failed searches were reported rather than being treated as an empty market.

The subsequent [GitHub verification run](https://github.com/KSI-Montenegro/estScra/actions/runs/41302569957)
passed: all six search feeds loaded, one listing from each source was exported, no errors were
reported, and checkpoint persistence plus artifact upload succeeded. Realitica and Estitor
listing captures used the Browser API; Patuljak used direct HTTPS. The test was one page per
search and three captures total, not a full-site crawl. The daily workflow is active.

A separate GitHub-hosted runner test worked for Patuljak, but Realitica and Estitor returned
direct HTTP 403 there. The workflow correctly saved a partial export and retry state, then
reported a failed run. The owner has since added `BRIGHTDATA_API_KEY`; the configured Browser
API fallback now reaches protected pages from GitHub. Direct-only mode still encounters those
blocks. Alternatively schedule the script on a server where all sources accept direct requests.

## Daily files

Every completed run writes a dated ZIP under `exports/` (repository: `daily_scraper/exports/`):

- `listings.json`: source text, details, field provenance, contact numbers exposed as `tel:` links, original image URLs.
- `listings.csv`: spreadsheet-friendly equivalent, with formula injection protection; exact source strings remain in JSON.
- `image_urls.csv`: all captured photo URLs grouped by property and image number.
- `watermark_samples.csv`: one representative untouched photo per new listing, queued for visual checking.
- `summary.json`: counts, discovery coverage, request usage and backlog.
- `errors.json`: safe error codes; failed listings remain queued for a future run.

The workflow now continues into photo processing. See [DAILY_PHOTOS.md](DAILY_PHOTOS.md) for
the complete sampling, local removal, optimization, R2 upload and queue behavior. Platform
imports should use the resulting `listings_with_photos.json` / `.csv`, which contain actual
sample outcomes and ready R2 image URLs.

Some Realitica ads expose only a small property-specific preview. Those observed URLs are
retained with `only_low_resolution_preview_exposed` in JSON quality flags; full-size paths
are never guessed. Other missing data stays missing and is flagged for review.

The listing scraper's initial `watermark_samples.csv` records `not_checked`. The connected
photo worker performs sampling and writes actual outcomes to its enriched listing exports,
per-property records and exception reports. Existing cleaned selections are preserved.

## State and coverage

`runtime/state.json` is essential. Keep it between runs. A file lock prevents simultaneous
local runs against the same state. Successful captures are deduplicated using source + ID;
failed captures retry, excess discoveries remain pending, and interrupted unexported captures
are recovered into a later export. Do not replace a damaged state with an empty file.

The supplied baseline seeds all 1,797 existing imported listing IDs. It is not a full historical
inventory. The first run will also find older ads not present in that selected import.
`first_seen_at` means first seen by this collector; source publication dates are separate fields.
Do not label all previously unseen ads as newly published.

Discovery scans Montenegro sale, long-term rent and holiday searches on Realitica, the property
category on Patuljak, and Montenegro sale/rental searches on Estitor. Estitor uses the verified
public `?sort=latest` control; Realitica uses `qob=p-new`. Patuljak's ordering includes promoted
ads, so the crawler never assumes numeric IDs or the first listing identify a publication cutoff.
Estitor's current sitemap has category pages rather than listing URLs, so it is not used as an ad feed.

The scan is bounded by `--max-pages` per search; it is not a guaranteed census of every ad.
`--overlap-pages 3` stops after three fully known pages; set 0 to disable this heuristic.
Raise page depth for high-volume periods or run a periodic deeper scan. Repeated pages,
empty/changed search markup, caps, and request failures are reported. Review the daily
`discovery`, `pending_backlog` and `errors` fields rather than treating an empty export as proof
that no new ads exist. The 300-capture daily cap can create a backlog on initial catch-up.

## Unattended GitHub schedule

The repository workflow `.github/workflows/daily-property-scrape.yml` runs at **07:17 UTC daily**
(08:17 Montenegro winter time, 09:17 summer time), and supports manual Run workflow.

One-time setup:

1. To enable Bright Data fallback, in repository Settings → Secrets and variables → Actions, add repository secret `BRIGHTDATA_API_KEY`. Without it, the workflow runs direct HTTPS only.
2. Optionally add repository variable `BRIGHTDATA_ZONE` for the HTTP zone and `BRIGHTDATA_BROWSER_ZONE` for the Browser API fallback.
3. Ensure GitHub Actions is enabled and the workflow is permitted to write repository contents.
4. Run “Daily property export” manually once and verify the summary/artifact.

State lives on the separate `daily-scraper-state` branch. The workflow never commits daily exports
to the main branch. Its state branch may contain captured source content while recovering an
interrupted export; use a private repository if that source data must remain private.
Daily CSV/JSON/ZIP files are attached as downloadable workflow artifacts, retained for 30 days.
The assistant's current GitHub integration can dispatch and inspect workflow runs, but cannot
write Actions secrets (the public-key endpoint returns HTTP 403). The repository owner has
configured `BRIGHTDATA_API_KEY` through GitHub Settings; the test job confirmed it is available.
No API key is embedded in the portable download.
The state restore fails closed on a network/JSON error. Concurrent scheduled/manual jobs are serialized.
State-save failures fail the workflow and must be fixed before another run to prevent duplicate exports.

GitHub schedules can be delayed, and public-repository schedules may be disabled after 60 days
without repository activity. This is a scheduled worker, not a promise that the chat assistant runs daily.
For a server instead, configure cron to invoke the script with persistent storage and credentials
already supplied securely; capture exit status 2 as a partial export that needs review.

## Checks and packaging

```bash
python -m unittest discover -s daily_scraper -p 'test_*.py' -v
python daily_scraper/build_bundle.py
```

The ZIP contains the script, its two existing parser modules, baseline, dependencies, tests and
workflow template. It excludes API keys, model weights, raw page caches and runtime credentials.
