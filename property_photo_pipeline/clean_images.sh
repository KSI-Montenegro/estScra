#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
py=watermark_detection/.venv/bin/python
"$py" property_photo_pipeline/prepare_removal_queue.py
set +e
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE "$py" property_photo_pipeline/remove_local.py \
  --candidates property_photo_pipeline/staging/local_removal_inputs/candidates.csv \
  --output property_photo_pipeline/staging/local_removal \
  --model property_photo_pipeline/models/big-lama.pt \
  --execute --workers 2 --threads 2 --context 96 --max-images 4789 --upload
worker_status=$?
set -e
# Exit 2 means the batch completed with per-image failures; these remain queued.
# Other failures may have interrupted uploads or prevented a complete summary.
if [[ "$worker_status" != 0 && "$worker_status" != 2 ]]; then
  echo "Repair batch stopped before a complete result; catalog was not updated." >&2
  exit "$worker_status"
fi
"$py" property_photo_pipeline/sanitize_repairs.py
"$py" property_photo_pipeline/build_triage.py
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE "$py" property_photo_pipeline/publish_catalog.py --execute
