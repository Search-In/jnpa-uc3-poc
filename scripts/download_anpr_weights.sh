#!/usr/bin/env bash
# ===========================================================================
# One-time downloader for the YOLOv8 license-plate detector weights.
#
# Pulls the publicly-released license_plate_detector.pt from the
# computervisioneng ANPR repo (the author ships it as a GitHub release asset /
# Google-Drive link). If the download fails (offline / moved), the ANPR service
# still runs in degraded mode (classical detector) — see ai/anpr/src/anpr/detect.py.
#
# Output: ai/anpr/resources/license_plate_detector.pt
#
# Usage:  scripts/download_anpr_weights.sh
#   Override the source with ANPR_WEIGHTS_URL=<direct .pt url> scripts/download_anpr_weights.sh
#   Print the SHA-256 afterwards so you can pin ANPR_YOLO_SHA256 in .env.local.
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEST_DIR="${REPO_ROOT}/ai/anpr/resources"
DEST="${DEST_DIR}/license_plate_detector.pt"
mkdir -p "${DEST_DIR}"

# Candidate direct URLs (first that responds 200 wins). The author has published
# the weights as a release asset; mirrors are listed as fallbacks. Operators can
# always supply a direct URL via ANPR_WEIGHTS_URL.
CANDIDATE_URLS=(
  "${ANPR_WEIGHTS_URL:-}"
  "https://github.com/computervisioneng/automatic-number-plate-recognition-python-yolov8/releases/download/v1.0/license_plate_detector.pt"
)

if [ -s "${DEST}" ]; then
  echo "  weights already present: ${DEST}"
else
  ok=""
  for url in "${CANDIDATE_URLS[@]}"; do
    [ -z "${url}" ] && continue
    echo "  trying ${url}"
    if curl -fsSL -m 180 -o "${DEST}" "${url}"; then
      ok="${url}"
      break
    else
      echo "    failed"
      rm -f "${DEST}"
    fi
  done
  if [ -z "${ok}" ]; then
    echo "  could not download weights — the ANPR service will run in degraded mode."
    echo "  set ANPR_WEIGHTS_URL to a direct .pt URL and re-run, or place the file at:"
    echo "    ${DEST}"
    exit 0
  fi
  echo "  downloaded <- ${ok}"
fi

# Print the SHA-256 so it can be pinned for startup hash-verification.
if command -v sha256sum >/dev/null 2>&1; then
  HASH="$(sha256sum "${DEST}" | awk '{print $1}')"
else
  HASH="$(shasum -a 256 "${DEST}" | awk '{print $1}')"
fi
echo "Done. ${DEST}"
echo "SHA-256: ${HASH}"
echo "Pin it:  ANPR_YOLO_SHA256=${HASH}  (add to .env.local)"
