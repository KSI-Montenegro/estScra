# Unattended OCR-assisted watermark pipeline

We already have 14,522 direct image URLs mapped to property IDs. Listing pages do not need to be scraped again. The worker accepts the image URL, downloads/caches the image internally and processes that copy. New image URLs can enter the same queue as new listings arrive.

## Recommended stages

1. **Capture and cache.** Preserve the original file, URL, property/image ID, dimensions and content hash. Analyze and edit that same captured file so masks cannot drift if the remote URL changes.
2. **Detect text locally.** `ocr_scan.py` runs installed Tesseract OCR on a grayscale image and a contrast-enhanced footer. It reports text, confidence and bounding boxes in original-image coordinates. It has no per-image OCR API fee or hosted OCR quota; CPU, storage and network resources still apply. A hosted OCR API can also fetch a public image URL if that provider supports URL input, but it would still download the pixels and have its own free-tier limits.
3. **Apply verified watermark rules.** OCR is not a watermark classifier. A logo/text match plus an established logo template, expected position and source-specific layout identifies an editing candidate. The Patuljak footer from the earlier one-image trial supplies one verified example. Other agencies/sites need a reference/policy once, not inspection of every photo. Existing recurring-pattern detection is supporting evidence only; its candidates include false positives. Never remove every OCR word: a sign, wall print or text on a TV can belong to the actual property.
4. **Build the smallest watermark mask.** Include the complete overlay and any mascot/outline/shadow, and exclude unrelated photo detail. Reject or skip ambiguous/overlarge masks automatically. A word box alone can miss the rest of a logo.
5. **Reconstruct only the masked area.** Use a configurable lower-cost image-editing API for confirmed cases, with an explicit batch limit and budget. OCR does not remove anything and is not the editing service. Small marks over plain surfaces may be candidates for free classical inpainting, but large logos over furniture/textures can produce obvious smears and should not be treated as equivalent to an image edit. The existing chat imagegen tool has no cheaper-model selector or bulk pricing controls.
6. **Composite and automatically check.** Preserve source dimensions. Copy the generated replacement only into the allowed mask and retain original pixels elsewhere; save losslessly as PNG. Verify exact pixel equality outside the effective mask, rerun OCR/template detection on the repaired region, and reject residual marks or obvious seam failures. Pixels hidden by the logo must be reconstructed: neither OCR nor these checks can prove that hidden detail matches the original scene.
7. **Export per property.** Store edited assets in the application's image/object storage, retain original URLs/hashes and the edit log, and export replacement image URLs grouped by property. Do not publish an edit that failed checks. This is a future storage integration, not a promise that thousands of binary photos should be committed to GitHub.

## How to avoid reviewing every image

Use a conservative unattended policy:

- Known watermark + reference/template + expected placement: eligible for the masked edit stage after the rule is configured.
- Unknown text, questionable location, missing detection, or inconsistent evidence: leave the original untouched and log it; optionally exclude it from a cleaned-only export.
- Failed edit or failed QC: keep the original and skip the edited version.

This removes the need to inspect each photo. It deliberately leaves uncertain cases unresolved, rather than claiming every watermark is removed. Review a few examples when establishing or changing a logo policy, and sample completed outputs. A fully free OCR API does not provide semantic certainty or free, faithful reconstruction of covered furniture.

## Runnable OCR stage: available now

`ocr_scan.py` is implemented and tested. Paid editing, mask approval/compositing and object-storage publication are not implemented by this script, and it never approves or edits images automatically.

Dependencies: the existing watermark Python environment plus the `tesseract` executable and English language data. Tesseract 5.5.0 is already installed in this cloud environment. Unlike the earlier purely classical detector, this OCR stage uses Tesseract's local trained LSTM character-recognition model. It does not call a paid vision LLM or hosted OCR/image-editing service.

From the repository root, run a balanced sample:

```bash
watermark_detection/.venv/bin/python watermark_detection/ocr_scan.py --sample 60 --output watermark_detection/runs/ocr_pilot
```

Run the complete URL manifest when ready:

```bash
watermark_detection/.venv/bin/python watermark_detection/ocr_scan.py --output watermark_detection/runs/ocr_full
```

Add `--brand 'agencyname'` for additional search terms. This only finds candidate text; it does not configure an approved removal rule. Downloads and OCR results are cached. Use a new output directory for different scan configurations. Requests have timeouts, bounded retries and per-host pacing, and the downloader pauses a host on HTTP 429. OCR subprocesses have bounded timeouts and limited CPU threads.

Outputs include `ocr_results.csv`, `results.json`, `summary.json`, an append-only checkpoint and per-property CSVs. The JSON includes original-pixel word boxes and raw OCR confidence. These are not calibrated watermark probabilities. A `no_text_detected` result is not proof of an unwatermarked image.

## Validation and actual scope

The pilot processed 60 images, not the full 14,522. It found four known-brand text candidates, 39 images with other recognized text, and 17 with no recognized text above the reporting threshold. These are text-finding counts, not verified watermark counts. The original full-frame OCR pass missed the visibly confirmed Patuljak example; footer thresholding and automatic OCR resolution recovered the word `patuljak` at the expected bottom-right location. This demonstrates why OCR alone should not authorize removal.

Thirteen controlled checks passed across the existing computer-vision detector and the OCR tests, including an actual Tesseract invocation, coordinate mapping, known-brand recognition, ordinary scene text and low-confidence rejection. Real-world accuracy has not been measured against human labels.

The pilot made zero hosted OCR calls and zero image-editing calls, and approved zero photos for editing. No original or property export was modified. `ocr_pilot/` contains the saved current pilot reports; caches and installed environments are excluded from Git.

The next production decisions are the verified logo/mask policies, a configurable edit backend and budget, and where GRADEN stores the resulting image assets. Only the matching, approved subset should incur editing charges.
