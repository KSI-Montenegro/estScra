# Daily photo processing and R2 delivery

The **Daily property export** GitHub workflow now runs the listing scraper and this photo pipeline.
No browser extension, hosted OCR service or paid image-editing API is used for photo processing.
The existing Bright Data key can fetch an image through its HTTP API if direct image access returns 403.

## Processing policy

1. Save listing photo URLs into a separate durable photo queue before processing images.
2. Read the current R2 catalog. Reuse accepted cleaned images and keep their URLs selected.
3. Check **one untouched source image per property**, preferring the first unprocessed image.
4. A negative OCR/template sample becomes `sample_passed`: its gallery may proceed provisionally.
   Only that one image was checked; this is not a certificate for every gallery photo.
5. A known brand/logo or uncertain recognized text flags the property's remaining unprocessed images.
   Existing known watermark flags are retained even if the sample is negative.
6. For flagged galleries, locate a supported mask independently on each image. The calibrated
   Patuljak footer policy is supported. Unknown marks and uncertain masks remain held.
7. Run local CPU LaMa only inside eligible masks. Check dimensions, exact outside-mask equality
   before lossy encoding, residual known-logo matching and focused brand OCR.
8. Optimize to WebP, targeting 300 KB with a **400,000-byte ceiling**, then upload immutable source
   and accepted cleaned versions to their property folders.
9. Merge the catalog using an owned pointer and ETag condition. Previously selected cleaned images
   cannot be replaced by source versions or a failed edit.

Detection uses local Tesseract and classical template matching. Ordinary scene text is never erased
merely because OCR recognized it. Unknown text can cause a conservative review flag. LaMa reconstructs
covered pixels; automated checks do not prove recovery of hidden scene details or detect every watermark.

## Files and entry points

- `photo_queue.py`: lightweight, secret-free durable input queue.
- `process_photos.py`: sampling, gallery processing, local repair, encoding, upload and exports.
- `photo_catalog.py`: bucket budget, immutable objects, checked uploads and catalog merge/publication.
- `photo_backfill.json`: the 316 listings exported before the photo workflow existed.
- `property_images/all_image_urls.csv`: existing property galleries queued for sampling on first initialization.

`photo-state.json` lives beside the listing `state.json` on the **daily-scraper-state** branch.
The listing and photo checkpoints are saved even after a processing failure. Completed image deliveries
are reused; limits, interrupted publication and transient downloads remain queued. New daily inputs
take priority over older backfill and legacy galleries. Three failed downloads become review exceptions.

The initial catch-up contains about 2,000 property galleries. It is intentionally processed in batches;
connecting the workflow does not imply that this backlog was completed by a three-property trial.

## What the platform should import

The original listing CSV/JSON remains the source capture. Import **`listings_with_photos.json`** or
**`listings_with_photos.csv`** from the photo outputs for delivery results:

- `source_image_urls`: original scraped URLs, preserved for provenance.
- `image_urls`: only ready R2 image URLs, in gallery order.
- `photos`: full per-image details, including held source copies (JSON).
- `photo_delivery_status`, `ready_image_count`, `held_image_count`: current queue/delivery result.
- `watermark_sample_result`, `watermark_sample_url`: the actual sample outcome.

When reading `properties.json`, render only images with **`ready_for_listing: true`**. An `active_url`
on a held source copy is an audit/download reference, not permission to put it in a ready gallery.
Existing accepted cleaned selections are ready. `sample_passed` source versions are provisional.

The current complete image catalog remains at:

`https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/property-photos/latest.json`

The latest daily listing/photo outputs are indexed at:

`https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/property-photos/daily/latest.json`

Daily assets include enriched listings, per-property photo records, `images.csv`,
`watermark_exceptions.csv`, `property_queue.csv` and `summary.json`. The GitHub artifact also includes
`property-photos.zip` with reports grouped by property. Binary images are delivered through R2, not
copied into Actions artifacts. Original downloads, intermediate PNGs, credentials and model weights
are excluded from the reports/artifacts. The old removal-progress object describes the original
4,789-candidate batch; the catalog and daily indexes describe ongoing delivery.

## Credentials and limits

The owner supplies three GitHub Actions secrets: `BRIGHTDATA_API_KEY`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`. Bucket endpoint/name/public delivery URL come from the nonsecret R2 config.
The first workflow run installs Tesseract, CPU PyTorch and the pinned 206 MB LaMa model; subsequent
runs use caches. No new editing subscription or Bright Data zone is created.

Default per-run limits are 300 property galleries, 1,500 images, 300 local edits and 20 image API
fallback requests. The listing scraper has its own 100-request Bright Data limit. These are count
limits, not monetary budgets. Run-workflow inputs can reduce photo limits for trials. The bucket
guard is 8 GB, below the owner's 10 GB storage allowance; a budget failure stops further publication.
Queued galleries resume on later runs. Uncertain/unsupported images remain visible in exception reports.

## Local run from the repository root

```bash
python -m pip install -r daily_scraper/photo-requirements.txt
python -m pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0+cpu'
python property_photo_pipeline/install_local_lama.py
python daily_scraper/photo_queue.py
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE python daily_scraper/process_photos.py \
  --execute --upload --max-properties 3 --max-images 40 --max-clean-images 20
```

Provide credentials through secure environment variables. For a standalone extracted ZIP, use
`photo-requirements.txt` and `process_photos.py --exports exports --state runtime/photo-state.json
--output photo_exports` instead of the repository-prefixed paths.

The historical `publish_catalog.py` snapshot publisher refuses to replace a catalog managed by this
pipeline: doing so would discard later daily additions. Use the unified photo worker going forward.
