# Non-AI watermark detector

A runnable classical-computer-vision script for the property image URLs already collected in this repository. No neural networks, image-analysis services, OCR, model weights, embeddings, or Bright Data API calls are used.

## What it can detect

**Known watermark templates:** provide a tightly cropped example of an actual watermark/logo. The script compares edge shapes at several scales, across the image, and marks strong matches. This is the more targeted method. A match establishes the supplied pattern, not a legal/copyright status.

**Unknown recurring overlays:** the script looks for stable edge shapes that recur at fixed positions across photos of different properties from the same source and similar aspect ratio. It checks all four corners and a central region. At least six distinct properties and nonduplicate photos must support a pattern. Only one reference photo per property is used; near-duplicate photos are filtered with a DCT perceptual hash. Stable masks are computed with simple pixel counts, not a trained model. The frequency threshold applies across each source/aspect reference group: this chiefly catches prevalent fixed branding. Minority agency logos can be missed even when they appear in several listings; use known templates for those.

This second method is a triage heuristic. A repeated shape can be a logo, date stamp, frame, or repeated scene feature. Faint, one-off, moving, rotated, large diagonal, heavily distorted, or varying watermarks may be missed. Templates work best when the actual watermark's appearance is known. Scores are correlation/edge-agreement measures, not calibrated watermark probabilities. No match is never proof that an image is clean.

## Tested pilot

The script processed 60 real image URLs, balanced across the three sources, using different properties. No known-logo templates were supplied. It flagged 28 recurring-overlay candidates and marked 32 images as insufficient reference. These are not confirmed watermark counts. No AI was used to inspect the photos, and real-image accuracy has not been measured against human labels. Eight controlled tests passed for known-template matching/localization, scaled logos, negative/blank fixtures, recurring-overlay detection, duplicate/property safeguards, and sample selection.

The full 14,522-image dataset has **not** been scanned. `pilot/summary.json` records the exact scope. The pilot snapshot includes results, per-property CSVs, repeated edge masks and annotated evidence copies for human inspection.

## Installation

From the repository root:

```bash
python -m venv watermark_detection/.venv
watermark_detection/.venv/bin/python -m pip install -r watermark_detection/requirements.txt
```

All dependencies are pinned to the versions exercised in this environment. No image model is downloaded. The existing manifest is `property_images/all_image_urls.csv`.

## Run

Balanced 60-image pilot:

```bash
watermark_detection/.venv/bin/python watermark_detection/detect_watermarks.py --sample 60 --output watermark_detection/runs/pilot
```

Scan all 14,522 image URLs for recurring overlays:

```bash
watermark_detection/.venv/bin/python watermark_detection/detect_watermarks.py --output watermark_detection/runs/full
```

For targeted detection, manually crop a known watermark from an original image, save it in the ignored `watermark_detection/templates/` directory, then run:

```bash
watermark_detection/.venv/bin/python watermark_detection/detect_watermarks.py --template watermark_detection/templates/agency_logo.png --output watermark_detection/runs/known_logo
```

Repeat `--template PATH` for multiple logos. Both methods run by default when templates are supplied. Add `--skip-repeat` to use only template matching. Template crops should exclude as much unrelated photo background as possible. Do not crop a red/orange evidence rectangle into the reference template.

Threshold controls: `--template-threshold` (default 0.72), `--min-properties` (6), `--repeat-frequency` (0.75), and `--repeat-threshold` (0.90). Lower thresholds increase both recall and false positives. Synthetic tests demonstrate the mechanics, not production accuracy. Review the evidence and calibrate on manually labeled examples before relying on flags for automatic filtering.

## Output

- `watermark_results.csv`: each selected image URL, property key/image number, status, reported match scores and evidence path.
- `by_property/PROPERTY_KEY/images.csv`: image findings grouped by property.
- `results.json`: detailed matches, coordinates and source URLs.
- `evidence/`: annotated copies of flagged images for manual review.
- `repeat_masks/` and `repeat_models.json`: recurring edge-pattern evidence.
- `summary.json`: selected/analyzed image counts, available comparisons, method, thresholds and limitations.
- `checkpoint.jsonl`: append-only attempt log, with scan IDs; the latest CSV/JSON are the current result snapshot.

Statuses:

- `template_match`: supplied pattern matched above threshold.
- `repeated_overlay_candidate`: recurring shape found; human review needed.
- `no_match`: the available comparisons did not produce an above-threshold match; it does not mean watermark-free.
- `insufficient_reference`: no stable repeat pattern/reference group and no supplied templates; inconclusive.
- `download_error` / `decode_error`: no valid classification could be made.

Coordinates refer to EXIF-oriented image pixels for template matches; repeat regions use a documented 640×480 comparison canvas. Reports map back to the original property and image URLs. Evidence paths are relative to the run-output root, including in per-property CSVs. Existing source files/listing exports are left unchanged, and no images are deleted or watermarks removed.

Downloads use bounded timeouts, one retry for transient errors, four workers by default, a per-host pacing interval and a pause on rate limiting. The cache avoids re-downloading on subsequent runs; detection is recomputed so repeat support reflects the complete selected batch. Packed edge features keep full-batch memory bounded. Use distinct output directories for different scan configurations. Downloaded originals, caches, templates and virtual environments are ignored by Git.

Run the tests:

```bash
watermark_detection/.venv/bin/python -m unittest discover -s watermark_detection -v
```
