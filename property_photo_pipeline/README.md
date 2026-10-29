# Property photo preparation and R2 upload

This tool prepares **local originals with explicit watermark clearance**, then audits and optionally uploads the resulting WebP files. It does not remove watermarks, make a clearance decision, download images, or call a generative service. An OCR `no_text` result is not clearance. Record a verified policy result or successful removal quality-control result for the exact original bytes; clearance does not belong to a URL or listing. Policy rules may be applied automatically after verification, and uncertain images must be quarantined. This tool does not implement that verifier or removal.

Install Pillow with WebP support for preparation and audit; install boto3 for actual upload. Python 3.9 or newer is required. Run the commands from the repository root:

```sh
python -m pip install -r property_photo_pipeline/requirements.txt
```

## Free preparation while removal is deferred

`stage_cached.py` can follow the full OCR scan and provisionally encode the downloaded originals locally. This costs no hosted OCR or editing API calls. It preserves any existing watermark and retains the OCR status; every result stays `publishable=false` pending a verified policy or successful removal QC. Files live under `staging/unverified/properties/<source>/<listing_id>/` and cannot be passed directly to the upload worker as a cleared manifest. Original bytes are unchanged. A watermarked photo must be removed and re-encoded later; an encoding from this stage does not substitute for that work.

```sh
python property_photo_pipeline/stage_cached.py --watch
```

Run OCR first. The staging worker follows its checkpoint, verifies the exact source hash, uses a bounded worker queue and reuses existing encodings on restart after verifying their hashes. `staging/progress.json` reports live counts; `summary.json` and `staging_inventory.csv` are written when it finishes. Without `--watch`, it processes the currently available checkpoint and exits. Unknown or unavailable photos remain flagged. Output files are private local work files and excluded from Git.

Create a clearance CSV and, if using files outside the downloader cache, a local mapping CSV:

* A local mapping CSV with columns `image_url,local_path`. `image_url` must match the URL in `property_images/all_image_urls.csv` exactly; each URL must occur once in the mapping. Paths can be absolute or relative to the mapping CSV. Originals are read only.
* A clearance CSV with columns `original_sha256,decision,decision_source`. SHA-256 must be the 64-character digest of the original file bytes; `decision` must be `cleared`; `decision_source` must identify the verified policy or removal quality-control record, for example `policy:rule-v1:result-123`. Duplicate hashes are rejected. OCR and `no_text` sources are rejected. The operator is responsible for the accuracy of this record.

To use the existing downloader cache without creating a local mapping CSV, replace `--local-files local-files.csv` with `--cache-dir watermark_detection/cache`. The tool resolves each URL to the downloader’s SHA-256 URL prefix filename and skips missing files.

The input listing manifest needs columns `property_key,listing_url,image_number,image_url`, matching the supplied repository manifest. The property key is `source_id`, using letters, digits, and hyphens in each part. To run a trial, make a small subset of the listing CSV containing only local, cleared images:

```sh
python property_photo_pipeline/pipeline.py prepare \
  --manifest trial-listings.csv --local-files local-files.csv \
  --clearances clearances.csv --output ./photo-ready
python property_photo_pipeline/pipeline.py audit \
  --manifest ./photo-ready/manifest.json --clearances clearances.csv
python property_photo_pipeline/pipeline.py upload \
  --manifest ./photo-ready/manifest.json --clearances clearances.csv
```

The last command is a dry run and requires no credentials. `prepare` emits `manifest.json`, `summary.json`, per-property manifests, and images under `properties/<source>/<id>/<number>-<original SHA256>.webp`. It reports and skips missing, invalid, or uncleared inputs and exits nonzero if any row was skipped. Review the summary before proceeding. Duplicate original bytes within a property are represented by the lowest numbered image and retain the duplicate URLs in the manifest. Identical files in different properties retain separate property paths. The original file, its SHA-256, the decision source, output dimensions, byte count, output SHA-256 and encoding quality are recorded. Keys are deterministic for the same listing and original bytes. Keep the original and ready files in place for later audits and uploads. Use a fresh output directory for each input set; old property manifests and ready files are not deleted on rerun.

EXIF orientation is applied, metadata is stripped, and the image is encoded as WebP without upscaling. By default the target is 300,000 bytes, the hard maximum is 400,000 bytes, and the minimum encoding quality is 75. When the original dimensions fit the hard maximum, they are preserved even if the target cannot be reached. Otherwise the image is progressively resized until it fits; an image is skipped if the hard limit cannot be met at the quality floor. Adjust `--target-bytes`, `--max-bytes`, and `--quality-floor` for a specific run. WebP versions or optimization settings can produce different bytes for the same original; an existing key with different content is treated as a conflict, never silently overwritten.

For an authorized upload, set `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` securely in the environment, then add `--execute` to the upload command. Nonsecret project settings in `r2_config.json` already specify the owner's endpoint, bucket and public delivery URL; `R2_ENDPOINT_URL`, `R2_BUCKET_NAME` and `R2_PUBLIC_BASE_URL` can override those defaults. Do not place credentials in CSVs or command arguments. The tool rechecks all original hashes, clearance records and prepared hashes before uploading; it also rechecks each file immediately before its upload. It sets `Content-Type: image/webp`, immutable cache headers, and SHA-256 object metadata. Existing objects are skipped only when HEAD returns the expected SHA-256 metadata and byte count; a different object fails the run. Object creation is conditional so an object appearing after HEAD is not overwritten. The operation retries transient SDK errors and can be resumed. R2 bucket access and public delivery are configured separately; no ACL or public access is assumed. Pass `--public-base-url` to override the delivery URL for a run.

Run the local fixture tests with `python -m unittest discover -s property_photo_pipeline -v`. The tests use generated images and do not contact R2.

Uploads require permission to list the bucket. Before writing anything, the tool lists all bucket objects and sums their bytes, adds the sizes of new planned objects, and fails if the total exceeds `--budget-bytes` (default 8,000,000,000). Listing and HEAD failures stop the run. This is a bucket snapshot: other buckets may count against account limits, and concurrent writers may increase usage after the check, so it does not guarantee remaining within an account-wide free tier. The bucket and endpoint for this project can be configured without embedding secrets:

```sh
export R2_ENDPOINT_URL=https://56293bcae8874b5edecb11807ee67359.r2.cloudflarestorage.com
export R2_BUCKET_NAME=graden-property-photos
# Supply R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY securely in your environment.
export R2_PUBLIC_BASE_URL=https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev
```

The public base URL was supplied by the bucket owner; object delivery will be verified after the first authorized upload. The worker reads credentials from process environment variables, not the example file. Credentials added to environment settings must be applied to the running workspace before an upload can use them.

A completed upload produces `published.csv` and per-property `published.json` records. Public URLs are blank when no public base URL is configured.

For a size study that makes no clearance decisions and writes no publishable image files, use `python property_photo_pipeline/benchmark.py --input-dir watermark_detection/cache --limit 60 --output /tmp/photo-benchmark.json`. It encodes cached `.image` files in memory and records per-image size, dimensions, quality and original hash. This sample is not necessarily representative of the whole collection.
