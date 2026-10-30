# Property photo bank and watermark triage

These reports join the full OCR scan, any completed known-logo scan, the source listing manifest and the upload inventory by property/image number and exact captured SHA-256. See `summary.json` for the counts and generation time. An expected public URL describes the planned object path; only an `uploaded_url` with a confirmed upload status establishes that this worker published it.

- `all_images.csv`: every original image URL, property, source and optimized hash, planned bucket key, upload status and triage result.
- `watermark_candidates.csv`: the union of known-logo template matches and known-brand OCR hits, deduplicated by property/image number. This is a processing queue, not a certified count of every watermark.
- `pattern_groups.csv` and `pattern_samples/`: layout groups with representative evidence sheets. Check a few examples when validating a logo policy, rather than reviewing every photo.
- `per_property_triage.zip`: one CSV per property under `properties/<source>/<listing_id>/`.
- `unavailable_images.csv`: image URLs that could not be downloaded.

`known_logo_template_candidate` means the supplied Patuljak mascot matched a classical image-template threshold. It does not detect every agency logo or watermark style. OCR-only candidates remain useful when a logo is cropped, appears inside a screenshot, or is missed by the template. Ordinary scene text, floorplan labels and browser text must not be erased just because OCR recognized them. Neither `no_text_unverified` nor a negative template match certifies an image as clean. The queue does not approve any paid edit automatically.

## Batch workflow

1. Import all available optimized source copies to their property folders. Keep source hashes and detection statuses in the manifest.
2. Validate a detection rule and full overlay mask on representative examples of each known logo style. Use the template position to locate the entire text/mascot/outline, not just an OCR word box.
3. Once an editing backend and budget are selected, process matching images in a resumable batch; hold uncertain cases in an exception queue without interrupting the rest.
4. Check that the known logo is gone, preserve original dimensions, and composite only the approved region before delivery compression. Keep the source copy. Automated checks reduce review work but cannot prove exact recovery of scene detail hidden by a watermark.
5. Store accepted outputs at separate immutable `cleaned` object keys and update a per-property manifest to select the cleaned URL. Retry failed jobs from their recorded stage; leave all other jobs completed.

The import, free OCR/template detection, optimized staging and reports are implemented. Paid reconstruction, validated full-overlay masks, repair QC and automatic selection of cleaned URLs are later stages; they have not been executed.

From the repository root:

```sh
watermark_detection/.venv/bin/python property_photo_pipeline/upload_staged.py --execute
watermark_detection/.venv/bin/python property_photo_pipeline/build_triage.py
```

The uploader requires bucket-scoped `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` values in the running process environment. The endpoint, bucket and public base URL are already in `r2_config.json`. Environment settings now contain declarations for these runtime variables; review/save and publish that configuration, and apply the stored values to the running worker, before expecting authenticated R2 access. No secret values belong in these reports or Git.
