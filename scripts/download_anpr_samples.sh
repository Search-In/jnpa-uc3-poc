#!/usr/bin/env bash
# ===========================================================================
# One-time downloader for ANPR sample clips.
#
# Tries Creative-Commons Indian-highway dashcam sources in order and uses the
# first that responds 200. If none respond, falls back to generating synthetic
# 30s MP4 clips (static Indian plate at random positions/brightness) so the
# pipeline always has non-empty feeds.
#
# Output (./data/clips/):
#   cam_g1_entry.mp4  cam_g1_exit.mp4  cam_corridor_km5.mp4  cam_corridor_km30.mp4
#
# Usage:  scripts/download_anpr_samples.sh
# ===========================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLIPS_DIR="${CLIPS_DIR:-${REPO_ROOT}/data/clips}"
mkdir -p "${CLIPS_DIR}"

FILES=(cam_g1_entry.mp4 cam_g1_exit.mp4 cam_corridor_km5.mp4 cam_corridor_km30.mp4)

# Candidate landing pages (NOT direct file URLs). We do not hard-code one; the
# script probes each for a 200 and, if a direct CC video URL is configured via
# ANPR_SAMPLE_URLS (space-separated), prefers those. Pexels/Pond5 require an API
# key or interactive download, so in a headless PoC these usually fail the
# probe and we fall back to synthetic generation.
CANDIDATE_PAGES=(
  "https://www.pexels.com/search/videos/indian%20traffic/"
  "https://www.pond5.com/free"
)

probe() {
  local url="$1"
  curl -fsS -o /dev/null -m 10 -A "Mozilla/5.0 (jnpa-uc3-poc)" "${url}" 2>/dev/null
}

download_direct() {
  # If the operator provides direct CC .mp4 URLs, use them in order.
  local urls=(${ANPR_SAMPLE_URLS:-})
  [ "${#urls[@]}" -eq 0 ] && return 1
  local i=0
  for f in "${FILES[@]}"; do
    if [ "${i}" -lt "${#urls[@]}" ]; then
      echo "  downloading ${f} <- ${urls[$i]}"
      if ! curl -fsSL -m 120 -o "${CLIPS_DIR}/${f}" "${urls[$i]}"; then
        echo "    failed; will synthesize this one"
      fi
    fi
    i=$((i + 1))
  done
  return 0
}

synthesize() {
  echo "  synthesizing 30s clips via OpenCV..."
  # Prefer the project venv if present.
  local PY="python3"
  [ -x "${REPO_ROOT}/.venv/bin/python" ] && PY="${REPO_ROOT}/.venv/bin/python"
  CLIPS_DIR="${CLIPS_DIR}" "${PY}" "${SCRIPT_DIR}/_synth_clip.py" "${FILES[@]}"
}

echo "ANPR sample fetch -> ${CLIPS_DIR}"

reachable=""
for page in "${CANDIDATE_PAGES[@]}"; do
  if probe "${page}"; then
    echo "  source reachable (200): ${page}"
    reachable="${page}"
    break
  else
    echo "  source not usable: ${page}"
  fi
done

# Try operator-provided direct URLs first, else attempt nothing automatic from
# the landing pages (they are not direct media), then synthesize whatever is
# still missing.
download_direct || true

missing=0
for f in "${FILES[@]}"; do
  if [ ! -s "${CLIPS_DIR}/${f}" ]; then
    missing=$((missing + 1))
  fi
done

if [ "${missing}" -gt 0 ]; then
  echo "  ${missing}/${#FILES[@]} clip(s) missing -> synthesizing"
  synthesize
fi

echo "Done. Clips:"
ls -la "${CLIPS_DIR}" || true
