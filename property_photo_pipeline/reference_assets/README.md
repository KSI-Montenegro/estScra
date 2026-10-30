# Patuljak mascot reference

`patuljak_mascot.png` is a crop of the confirmed mascot in the locally captured reference photo described and hashed in `patuljak_mascot.json`. Its only purpose is locating possible matches. The matching tool uses normalized RGB template correlation in the lower 35% of the EXIF-oriented image at scales 0.5, 0.75, 1, 1.25, 1.5 and 2, with a default candidate threshold of 0.85. It records the best score and box even below that threshold. Cropping the reference for analysis does not modify the original or remove watermarks.

The 60-image pilot sampled the first 20 images per listing source: 17 Patuljak candidates and none from the two other sources. The exact reference scored 1.0; the two supplied non-logo comparison photos scored 0.4285 and 0.4219. This sample is small and these figures do not measure accuracy over the population. A `no_template_match` result is not evidence an image lacks a watermark: the logo may be elsewhere, at another size, different, or missed by the matcher. `known_logo_template_candidate` is an automated candidate, not a clearance or removal decision.

Run using the dependencies in `watermark_detection/requirements.txt`:

```sh
watermark_detection/.venv/bin/python property_photo_pipeline/match_known_logo.py --pilot-per-source 20 --output watermark_detection/runs/logo_match_pilot
watermark_detection/.venv/bin/python property_photo_pipeline/match_known_logo.py --output watermark_detection/runs/logo_match
watermark_detection/.venv/bin/python -m unittest discover -s property_photo_pipeline -p 'test_match_known_logo.py' -v
```

The tool verifies each available cached source against its recorded SHA-256, does not modify source images, and makes no network calls. It produces a resumable checkpoint, a CSV of scores and boxes, and a summary. The default four workers each use a single OpenCV thread. Failed inputs are reported and retried on a later run; missing sources are marked unavailable.
