# Run the full pipeline on your computer

Use Python **3.12** on Ubuntu/Linux, macOS, or Windows through **WSL2 Ubuntu**.
The scripts use `fcntl`, so native Windows Python is not supported. Run commands
from the repository root. No browser extension or web frontend is required.

## 1. Get the code

Fork https://github.com/KSI-Montenegro/estScra (select `main`), then clone your fork.
Or clone the original directly:

```bash
git clone https://github.com/KSI-Montenegro/estScra.git
cd estScra
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r daily_scraper/photo-requirements.txt
```

On Ubuntu/WSL, install system dependencies first:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv tesseract-ocr tesseract-ocr-eng
```

On macOS, install Python 3.12 and Tesseract with Homebrew. CPU PyTorch setup:

```bash
# Linux / WSL x86_64, same version as the GitHub runner:
python -m pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0+cpu'
# macOS: use this instead of the Linux command:
# python -m pip install 'torch==2.8.0'

python property_photo_pipeline/install_local_lama.py
```

The installer downloads the approximately 206 MB pinned LaMa TorchScript model
and checks its size and SHA256. No image-editing API is needed. Playwright connects
to the hosted Bright Data browser when necessary; local Chromium installation is
not required. Its WSS endpoint must be reachable on `brd.superproxy.io:9222`.
Other CPU architectures need a compatible PyTorch 2.8 wheel.

## 2. Configure local environment variables

Copy `.env.example` to `.env`, fill in your own values, then load it in your shell:

```bash
cp .env.example .env
# Edit .env in your editor. Do not commit it.
set -a
source .env
set +a
```

`BRIGHTDATA_API_KEY` enables paid fallback fetching; direct-only discovery works
without it on portals that accept your connection. `BRIGHTDATA_ZONE` is the HTTP
zone; `BRIGHTDATA_BROWSER_ZONE` is the separate Browser API zone. Use the existing
zones listed in the example only if they belong to your Bright Data account.
Patuljak phone reveals can require `PATULJAK_COOKIE`; see
[daily_scraper/PATULJAK_SMS_AUTH.md](daily_scraper/PATULJAK_SMS_AUTH.md) for the
optional TextBee renewal service.

Set all five `R2_*` values for your target bucket. The committed R2 config has the
existing GRADEN bucket as its fallback, so explicitly configure a separate bucket
when building an independent installation. Read/write R2 credentials are needed
only for upload/publication stages. Public `r2.dev` URLs are not S3 credentials.

GitHub Actions secrets are not readable locally and are not copied when forking.
Provide the same authorized credentials through your local secret manager or `.env`.

## 3. Choose existing or fresh state

To continue the current deployment without rediscovering its already captured
ads, restore all three checkpoints **before the first run**.
The commands below require a Git clone; use the clone route above when resuming
production rather than just extracting the source-only ZIP. A fork made with
only `main` does not contain this state branch; fetch it from the original:

```bash
git fetch https://github.com/KSI-Montenegro/estScra.git \
  refs/heads/daily-scraper-state:refs/remotes/graden/daily-scraper-state
mkdir -p daily_scraper/runtime
git show refs/remotes/graden/daily-scraper-state:state.json > daily_scraper/runtime/state.json
git show refs/remotes/graden/daily-scraper-state:contact-state.json > daily_scraper/runtime/contact-state.json
git show refs/remotes/graden/daily-scraper-state:photo-state.json > daily_scraper/runtime/photo-state.json
```

These commands overwrite local checkpoint files: use them only for initial
restoration into an empty runtime directory. Keep subsequent local checkpoints.
Fetching this branch is not required for an independent fresh installation.

A full clone includes historical seed inputs; the source-only ZIP omits them
except the listing-ID baseline. **For the source-only ZIP or an independent
installation without the old import, set
`GRADEN_SKIP_HISTORICAL_PHONE_BASELINE=1` in `.env`.** That explicitly compares
phones only against current cumulative records. Published summaries label this
reduced coverage. Full clones default to `0`: missing expected history is an
error, so production cannot silently stop checking its historical phone baseline. With no checkpoint, that ZIP starts fresh contact
and photo queues. Copy the original historical inputs separately if you need their
cross-portal phone comparison history. `first_seen_at` does not prove that an ad
was newly published.

Use one active writer for a shared deployment. When taking over production
locally, coordinate stopping its GitHub schedule and restoring the latest state;
do not run two independent schedulers against the same R2 feed/checkpoints.
Local file locks cover each stage, not two separate computers.

## 4. Try discovery without paid calls or R2 writes

```bash
python daily_scraper/daily_scraper.py --mode direct \
  --sources patuljak --max-pages 1 --max-listings 3 \
  --max-api-requests 0 --max-runtime-seconds 120
```

Exports go to `daily_scraper/exports/<run>/`; state stays in
`daily_scraper/runtime/state.json`. Source access challenges may require the
configured Bright Data fallback. Use `--mode auto` to enable it.

## 5. Run all stages with bounded limits

With your environment loaded and R2 target configured:

```bash
python daily_scraper/daily_scraper.py --mode auto \
  --max-pages 1 --max-listings 10 --max-api-requests 10

python daily_scraper/contact_enrichment.py --collect \
  --max-listings 10 --max-reveals 3 --max-api-requests 10

# Queue inputs are restored automatically by process_photos.py.
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE python daily_scraper/process_photos.py \
  --execute --upload --max-properties 3 --max-images 40 \
  --max-clean-images 20 --max-image-api-requests 5 --max-runtime-seconds 900

env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE python daily_scraper/contact_enrichment.py --publish
```

The first two stages collect data locally. `--upload` and `--publish` write to the
configured R2 bucket. Bright Data caps limit request count, not financial spend.
The local LaMa engine has no per-image API fee.

For photos only, run the last two commands against restored state. Setting
`--priority-properties patuljak_86717` changes the order of **pending** work; it
does not automatically re-edit completed or held files. An incident re-edit needs
an exact-source mask and a new output-bound review receipt. The latest 16 approved
86717 photos are already in R2; see the workflow handoff for their manifest.

## 6. Consume results and schedule

Use the cumulative R2 index `/graden-imports/listings/latest.json`, then follow its
`assets["listings.json"]` URL. Photo catalog index:
`/graden-imports/property-photos/latest.json`. Both are relative to your
`R2_PUBLIC_BASE_URL`.

Publish only eligible `image_urls` with `photos_ready_for_publication=true`.
Per-image `ready_for_listing`, source/output hashes and review holds remain
available for audit. Never fall back to original ad photos when ready photos are
missing. Local enriched outputs are under `daily_scraper/photo_exports/` and
`daily_scraper/contact_exports/`.

Use cron/systemd/your local scheduler to run the same stages sequentially, with
the repository as the working directory, the virtualenv Python and securely
loaded environment variables. The existing GitHub workflow is the reference:
07:17 UTC discovery daily; contacts hourly at minute 37; photo recovery every
three hours at minute 47. GitHub uses UTC; local cron uses the host's timezone
unless configured otherwise. Keep the runtime directory between runs.

## Verify code and package it

```bash
# Full clone (historical imports present):
python -m unittest discover -s daily_scraper -p 'test_*.py'
# Source-only ZIP: load .env with GRADEN_SKIP_HISTORICAL_PHONE_BASELINE=1 first.
python scripts/build_source_bundle.py
```

The source-only ZIP is written to `dist/estScra-source.zip`. It has the same folder
layout as the repository and includes the workflow, parsers, local repair code,
reference masks, tests and this guide. Runtime credentials and model weights are
never part of that bundle.
