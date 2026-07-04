#!/usr/bin/env bash
# Fetch the ArcFace face-recognition ONNX model for the identity service
# (IDENTITY_EMBEDDER=onnx). The file is git-ignored (≈166 MB) and mounted into the
# identity container at /models/arcface.onnx (see docker-compose.yml).
#
# Usage: scripts/fetch_face_model.sh
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data/models"
mkdir -p "$DEST"

# Download to a `.partial` temp and atomically move into place ONLY on success.
# A failed or disk-full (ENOSPC) download therefore never leaves a truncated file
# that the `[ -s ]` "already present" check would later skip — which would
# silently mount a corrupt ONNX model and crash-loop the identity service.
fetch() {
  local name="$1" url="$2" out="$3"
  if [ -s "$out" ]; then
    echo "$name already present: $out ($(du -h "$out" | cut -f1))"
    return 0
  fi
  echo "downloading $name -> $out"
  local tmp="$out.partial"
  rm -f "$tmp"
  if curl -fSL --retry 3 -o "$tmp" "$url"; then
    mv -f "$tmp" "$out"
    echo "done: $(du -h "$out" | cut -f1)"
  else
    rm -f "$tmp"
    echo "ERROR: failed to download $name (network error or no disk space)" >&2
    return 1
  fi
}

# InsightFace buffalo_l recognition model (w600k_r50), mirrored by immich-app.
fetch "ArcFace model" \
  "${ARCFACE_MODEL_URL:-https://huggingface.co/immich-app/buffalo_l/resolve/main/recognition/model.onnx}" \
  "$DEST/arcface.onnx"

# Anti-spoofing / liveness model (hairymax Face-AntiSpoofing, binary real/spoof,
# 128x128). Mounted at /models/antispoof.onnx; enable with IDENTITY_LIVENESS=true.
fetch "anti-spoof model" \
  "${ANTISPOOF_MODEL_URL:-https://github.com/hairymax/Face-AntiSpoofing/raw/main/saved_models/AntiSpoofing_bin_1.5_128.onnx}" \
  "$DEST/antispoof.onnx"
