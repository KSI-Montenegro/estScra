# Montenegro property scraper

Source code and the running GitHub workflow are on the **main** branch of
[KSI-Montenegro/estScra](https://github.com/KSI-Montenegro/estScra/tree/main).

Scrapes Realitica, Patuljak, Estitor and Oglasi.me; collects listing details and
advertiser phones; deduplicates by portal/listing ID; checks each photo for
watermarks; applies supported local LaMa repairs; optimizes WebP images; and
publishes ready images plus listing feeds to Cloudflare R2.

- **[Local installation and commands](LOCAL_SETUP.md)** — Python setup, credentials,
  restoring existing state, running stages and scheduling locally.
- **[Workflow YAML](.github/workflows/daily-property-scrape.yml)** — the actual GitHub Actions job.
- **[Current pipeline handoff](daily_scraper/WORKFLOW_HANDOFF.md)** — schedules,
  storage paths, per-image checks, agency routing and known review holds.
- **[Scraper code](daily_scraper/)** — discovery, contacts, photo queue and feed publication.
- **[Watermark removal code](property_photo_pipeline/)** and
  **[detection code](watermark_detection/)** — local image processing and calibrated masks.

Fork this repository using GitHub's **Fork** button. For a local copy:

```bash
git clone https://github.com/KSI-Montenegro/estScra.git
cd estScra
```

Code lives on `main`. Resumable production checkpoints live on the separate
`daily-scraper-state` branch. Forks do not inherit Actions secrets; configure your
credentials locally or in your fork. A local run does not need GitHub Actions.

Photo readiness requires checks of the exact delivered bytes. A `/cleaned/` path
or a passed cover sample alone does not authorize publication. Unsupported marks
remain held. See the handoff for the current policy and incident status.

The source-only bundle builder is `python3 scripts/build_source_bundle.py`.
It excludes captured listings, contact exports, runtime state, model weights and
credentials. The pinned model is downloaded separately during local setup.
