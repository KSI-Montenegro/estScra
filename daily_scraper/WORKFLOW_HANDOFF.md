# Property scraper and photo publication workflow

Updated 2026-11-13. Repository: `KSI-Montenegro/estScra`, workflow
`.github/workflows/daily-property-scrape.yml`.

## Schedule and durable work

- Daily discovery: 07:17 UTC (08:17 in Montenegro on this date).
- Contact retries: every hour at minute 37.
- Photo recovery: every three hours at minute 47 UTC, without new discovery.

The four portals are Realitica, Patuljak, Estitor, and Oglasi.me apartments/houses.
Stable portal/listing IDs prevent repeat capture. Descriptions, published fields,
phones and observed original image URLs are saved. Direct HTTPS is tried first;
the existing Bright Data transport is bounded fallback. Patuljak can renew its
account session through the private TextBee receiver. Account cookies/SMS codes
never enter public exports. Estitor reveal quotas retain their cooldowns.

`daily-scraper-state` holds `state.json`, `contact-state.json` and
`photo-state.json`. One workflow concurrency group serializes updates. Interrupted
work resumes from checkpoints. Normal daily photo limits remain configurable;
scheduled recovery defaults to 25 properties, 250 images, 25 edits, 900 seconds,
and at most five image proxy requests. The public repository uses standard hosted
CPU runners and local LaMa; there is no paid image-editing API.

## Why sample-required and cleaned files were unsafe

The old scraper assigned `sample_required` when it captured source URLs. Photo
processing was separate, bounded, and skipped by hourly contact jobs. The listing
export did not consistently replace its old watermark status. A passed cover
sample could release unexamined gallery images. Legacy cleaning checked the
calibrated Patuljak region and focused OCR; it did not identify every agency mark
elsewhere in a photograph. Thus `/cleaned/` described an edit artifact, not proof
that the entire delivered image was clear.

The new policy checks every original independently, then checks each existing or
new cleaned image as delivered. Source and output SHA256 values bind the evidence
to exact bytes. The representative sample fields remain for old consumers, but
do not authorize the rest of a gallery.

Each photo has `watermark_status`: `clean`, `watermarked`, `cleaned`, or `unknown`.
`unknown` covers inconclusive detection, unavailable bytes, and unresolved review.
Unknown photos are held. Patuljak and Oglasi originals are not cleared solely
because OCR found no text when their expected stamp cannot be located. Existing
source flags cannot be cleared by a negative cover sample. Cleaned files require
matching per-image original and delivered checks before publication.

Per-image fields include `watermark_check_status`, `watermark_portals`,
`watermark_agencies`, `watermark_brands`, `watermark_analysis`,
`delivered_watermark_analysis`, `source_sha256`, `repair_output_sha256`,
`publication_hold`, and `ready_for_listing`.

Supported masks still use the pinned local LaMa model. Arbitrary OCR text does not
authorize erasure. Full delivered-image checks supplement existing mask/residual
checks. WebP delivery targets 300 KB and has a 400,000-byte ceiling. Original and
historical cleaned files remain in storage when readiness is revoked.

## Agency signals for Cowork

Portal stamps (`patuljak`, `estitor`, `realitica`, `oglasi`) are separate from
agency identities. A portal stamp alone never makes someone an agency.

The agency text registry currently includes Kvadrat, Seaside Living, Montenegro
Prospects, Domus and Puerta Real Estate. Recognized names include OCR evidence and
confidence. Faint lettering and purely graphical logos can evade OCR: unrecognized
marks remain unknown, and this registry does not claim to recognize every logo.
Further validated graphic references can be added with a policy-version update.

`all_photos_agency_branded=true` requires a completed audit and positive agency
evidence on every captured original photo, including accounting for missing files.
It sets `suggested_contact_queue=agents`. A partial sample cannot set that flag.
The five agency cases explicitly reported by the operator are also routed to
`agents`, with `agency_signal_status=operator_reported` and
`agency_routing_reason=operator_reported_agency_watermark`; those reports do not
fabricate per-image detections or an all-photos result. These are routing signals,
not verification of legal ownership.

## Incident containment

The reported properties are 86717, 79250, 64027, 72906, 72989, 82189, 5176,
77029 and 86379, all under `patuljak_`. Their nine current galleries were held in
the shared catalog and both listing feeds. The published 86717 draft used original
ad photos while scraper photos were pending; the operator already hid that listing.
The other reported residuals were in drafts. For 77029, screenshot/player graphics
were reported and remain a review issue rather than an invented agency identity.

Draft image numbers do not map reliably to original gallery numbers. Resolve
reported files by exact source/delivered URL and SHA256. The operator supplied 16 exact draft R2 URLs for 79250 and 64027. All 16 match
the current catalog and the downloaded SHA256 bytes: these are current stored
cleaned versions, not superseded variants. The exact matches are recorded in
`watermark_incident_validation.json`. This establishes file identity, not which
original positions carried a reported agency mark. Reported legacy edits remain
quarantined despite negative OCR until residuals are resolved.

## Storage and consumer contract

Bucket: `graden-property-photos`.
Public base: `https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev`.

- Cumulative feed: `/graden-imports/listings/latest.json`; read its `assets` URLs.
- Photo catalog: `/graden-imports/property-photos/latest.json`; read `catalog_url`.
- Images: `/properties/<portal>/<id>/source/…` and `/cleaned/…`.
- New feed assets: `watermark_audit.csv`, `agency_routing.json`,
  `agency_routing.csv`, alongside listings and normalized `phone_matches` reports.
- Global catalog assets also include `ready_images.csv`, `held_images.csv`, and
  `agency_routing.json`, covering historical properties outside the daily feed.

Publish from the listing feed's `image_urls` and per-photo `ready_for_listing`
flags. Original `source_image_urls` are provenance and audit inputs. A `/cleaned/`
path alone does not grant readiness. `photos_ready_for_publication` uses a minimum
of three eligible photos; other listing/contact checks still belong to the platform.
`photo_audit_status`, `photo_audited_count`, `photo_pending_count` and
`held_image_count` expose remaining work. The wider historical queue is resumable;
an initial incident audit is not a claim that all historical images are checked.

## Earlier failed run

Run 43423696302 failed after cleanup because the catalog verifier accepted only
Patuljak repair proofs. Oglasi proof validation was added, and recovery run
[43425482745](https://github.com/KSI-Montenegro/estScra/actions/runs/43425482745)
passed using the saved uploads. That historical run remains failed in GitHub; its
failure is distinct from the newly reported residual-watermark incident.

Per-image incident verification passed in
[run 43430196381](https://github.com/KSI-Montenegro/estScra/actions/runs/43430196381).
All 115 captured originals in the nine galleries were audited, along with all 91
saved cleaned outputs. Their holds remain: completing an audit does not certify
an unresolved file as clean. The five agency labels in this report are explicitly
operator-supplied; the automatic audit recognized Patuljak but did not reliably
read those faint agency marks. 145 regression tests passed. The wider legacy
re-audit continues through bounded recovery jobs.


## Reviewed redelivery of Patuljak 86717

On 2026-11-13, all 17 captured originals were reprocessed locally from their
SHA256-verified bytes, rather than resuming the 11 old cleaned artifacts. The
reviewed pose was the constant 293x103 bottom-right stamp, positioned at
`(width-294, height-103)`. The calibrated mask was expanded with a 9x9 dilation;
the pinned LaMa model inpainted only that mask with 128px context. Source dimensions
were preserved. Output files passed logo correlation (<0.65), focused brand OCR,
and individual full-image visual review. Ordinary scene text that made free OCR
inconclusive was resolved by the exact-output visual review, not silently ignored.

Sixteen property photos are now eligible. Image 17 is a floor plan: its technical
title block was distorted by the candidate repair, so it remains held and was
excluded from the delivery. The other eight incident galleries remain held.
WebP files are 29,840–153,960 bytes, with new content-addressed URLs. Both listing
feeds, the photo catalog and durable photo state were updated.

The `patuljak-footer-reviewed-v3-expanded` receipt binds original/output hashes,
model/mask identity, dimensions, residual checks and the visual review of the
exact delivered bytes. A new edit can replace an old cleaned selection only by
naming its exact `supersedes_output_sha256`; earlier URLs are retained as held
history. Stale worker output cannot reselect an old revision or attach old checks
to the new bytes. Future positive brand evidence still revokes readiness. These
reviewed receipts do not authorize automatic clearance of other incident files.

Delivery manifest: https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/properties/patuljak/86717/deliveries/155e46ee4e98f58613115dcb/images.json
Plain URL list: https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/properties/patuljak/86717/deliveries/155e46ee4e98f58613115dcb/image-urls.txt

151 regression tests passed, including matching/stale replacement rejection,
version history, incident holds and rerun handling of ordinary text versus a
new positive watermark detection.
