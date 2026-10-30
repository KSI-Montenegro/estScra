# Local LaMa repair pipeline

The owner selected local LaMa, with no per-image editing API fee. The website that prompted this choice describes LaMa as its engine but prohibits automated scripts in its terms; this pipeline does not submit images to that website. CPU PyTorch 2.8.0 is installed, and the release model is pinned in `lama_model.json`. Binary weights are excluded from Git.

```sh
watermark_detection/.venv/bin/python -m pip install -r property_photo_pipeline/requirements-local.txt
watermark_detection/.venv/bin/python property_photo_pipeline/install_local_lama.py
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE watermark_detection/.venv/bin/python property_photo_pipeline/run_cleanup.py
```

The supervisor prevents concurrent duplicate batches, checkpoints progress, and publishes a small live record at `https://pub-f3c0e65ac46b487d83f5dfb93b1c90b8.r2.dev/graden-imports/property-photos/removal-progress.json`. It runs enrichment, masked reconstruction, accepted-image upload, report refresh and catalog publication automatically. The 4,789-candidate batch completed: 4,664 accepted outputs were uploaded and selected in the catalog, with 125 candidates queued. Twelve visually checked pilot outputs have already been published separately; the progress record counts the current bulk run. The current catalog pointer selects accepted cleaned URLs and the remaining queue records exceptions.

The first three tight-mask results left visible text-shaped ghosts. A revised policy fills the word bands and expands the silhouette, calibrated from 48 aligned source images. Ten varied graphic-match examples and twelve weaker-graphic/OCR-correlated examples were then visually reviewed with no visible known footer logo remaining. This pilot is not a population accuracy measurement. Pixels outside the effective mask were checked equal before delivery compression.

`remove_local.py` prepares a small local trial using an existing TorchScript LaMa model and the separately supplied `watermark_mask.py`. It does not download a model, use a hosted editing service, or modify original cached images. The default is a dry run; the default trial limit is three candidates. The mask policy admits high-confidence mascot matches and lower-scoring mascot matches corroborated by independently located brand-text OCR. Other candidates remain queued.

```sh
python property_photo_pipeline/remove_local.py
python property_photo_pipeline/remove_local.py --execute --model /path/to/big-lama.pt --max-images 3 --threads 2
```

Use an environment containing CPU PyTorch, NumPy, Pillow, OpenCV and Tesseract, plus the project's mask module and reference template. The model must accept `(image, mask)` as float32 tensors of shapes `1x3xHxW` and `1x1xHxW`, with RGB values in `[0,1]`, and return an RGB tensor of the image shape or a dictionary with an `inpainted` tensor. No inference is attempted unless `--execute` and a model path are supplied. `--threads` allows 2 or 4 CPU threads; `--workers` allows 1 or 2 concurrent jobs and defaults to 1. Their product cannot exceed 4. `--context` sets the padding around the mask. `--max-images` can be increased explicitly for a later run after the trial is assessed.

Every source file must match the candidate's original SHA-256. An ineligible or empty mask is queued. The cropped input is padded to a multiple of eight; the generated pixels are composited into the mask only, with exact outside-mask equality checked before encoding. A lossless intermediate, per-image result, model and mask configuration hashes, per-job elapsed time, and checkpoint are saved under the ignored `staging/local_removal` directory. This exact equality applies to the lossless composite; the resulting WebP is lossy. The original dimensions are retained; if the 400,000-byte limit cannot be met at the quality floor without resizing, the image is queued.

Automated QC searches for residual reference mascot and runs local OCR inside the full overlay box plus four pixels. A detected residual is queued. These checks can miss defects or watermarks; `processed` means automated checks passed and is **not** semantic or visual certification. There is no automatic approval for unrelated scene text. A failed image stays in the checkpoint with its error or queue reason; successful outputs are reused only with matching configuration and hashes.

`--upload` may be combined with `--execute` to upload processed outputs to R2 after the bucket-size guard and hash checks. It uses the same R2 configuration and environment credentials as the provisional uploader, with the 8,000,000,000-byte bucket budget by default (adjust with `--budget-bytes`). Uploads use at most 16 concurrent requests after preflight and checkpoint each result. Object keys contain `cleaned/<number>-<output_sha256>.webp`, and metadata says `processed`, not certified. Originals and provisional source keys are untouched. As with the provisional uploader, the bucket check is a snapshot and is not an account-wide quota guarantee. Catalog changes are handled separately.

The word and `.me` gradient correlations in `residual_text_shape` are experimental measurements at the known graphic pose. A first trial left visible text-shaped remnants even though mascot and OCR checks passed, and the available clean comparison overlaps some residual scores. No text-shape threshold is currently validated; the numbers do not certify removal. The configuration hash includes the mask image and policy JSON, text reference crops, and metric code, so changes to those assets invalidate prior checkpoints.

After the pilot has been evaluated, `property_photo_pipeline/clean_images.sh` runs the authorized batch, exports an allowlisted public repair manifest, refreshes the full and remaining queues, and publishes an updated catalog. The catalog retains the source copy URL and selects a repaired URL only after source-hash, policy, automated-check, output-hash and upload-status validation. The catalog describes repaired images as processed with automated checks; it does not certify that all watermarks or other defects are absent. Fatal batch or publication failures stop the runner; completed batches with individual errors leave those images in the remaining queue.
