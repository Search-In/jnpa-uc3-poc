#!/usr/bin/env bash
# Fetch the ArcFace face-recognition ONNX model for the identity service
# (IDENTITY_EMBEDDER=onnx). The file is git-ignored (≈166 MB) and mounted into the
# identity container at /models/arcface.onnx (see docker-compose.yml).
#
# Usage: scripts/fetch_face_model.sh
set -euo pipefail

DEST="$(cd "$(dirname "$0")/.." && pwd)/data/models"
OUT="$DEST/arcface.onnx"
# InsightFace buffalo_l recognition model (w600k_r50), mirrored by immich-app.
URL="${ARCFACE_MODEL_URL:-https://huggingface.co/immich-app/buffalo_l/resolve/main/recognition/model.onnx}"

mkdir -p "$DEST"
if [ -s "$OUT" ]; then
  echo "ArcFace model already present: $OUT ($(du -h "$OUT" | cut -f1))"
else
  echo "downloading ArcFace model -> $OUT"
  curl -fSL --retry 3 -o "$OUT" "$URL"
  echo "done: $(du -h "$OUT" | cut -f1)"
fi

# Anti-spoofing / liveness model (hairymax Face-AntiSpoofing, binary real/spoof,
# 128x128). Mounted at /models/antispoof.onnx; enable with IDENTITY_LIVENESS=true.
SPOOF_OUT="$DEST/antispoof.onnx"
SPOOF_URL="${ANTISPOOF_MODEL_URL:-https://github.com/hairymax/Face-AntiSpoofing/raw/main/saved_models/AntiSpoofing_bin_1.5_128.onnx}"
if [ -s "$SPOOF_OUT" ]; then
  echo "anti-spoof model already present: $SPOOF_OUT ($(du -h "$SPOOF_OUT" | cut -f1))"
else
  echo "downloading anti-spoof model -> $SPOOF_OUT"
  curl -fSL --retry 3 -o "$SPOOF_OUT" "$SPOOF_URL"
  echo "done: $(du -h "$SPOOF_OUT" | cut -f1)"
fi
