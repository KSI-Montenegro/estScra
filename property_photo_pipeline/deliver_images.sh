#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
task_python="${GRADEN_PHOTO_PYTHON:-watermark_detection/.venv/bin/python}"
if [[ -z "${R2_ACCESS_KEY_ID:-}" || -z "${R2_SECRET_ACCESS_KEY:-}" ]]; then
  echo 'Set R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY securely before delivery.' >&2
  exit 1
fi
# These R2 calls supply their own explicit credentials and do not use AWS profiles.
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE "$task_python" property_photo_pipeline/upload_staged.py --execute
"$task_python" property_photo_pipeline/build_triage.py
env -u AWS_PROFILE -u AWS_DEFAULT_PROFILE "$task_python" property_photo_pipeline/publish_catalog.py --execute
