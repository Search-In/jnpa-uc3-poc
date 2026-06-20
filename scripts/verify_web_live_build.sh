#!/usr/bin/env bash
# Proof that the built dashboard bundle (web/dist) is LIVE, not mock.
#   - the live build marker  "JNPA_DATA_MODE:live"                 MUST be present
#   - the mock sentinel      "JNPA_MOCK_ADAPTER_PRESENT_DO_NOT_SHIP" MUST be absent
# Exit non-zero (fails CI / deploy) if either check fails.
#
# Usage:  scripts/verify_web_live_build.sh [web/dist]
set -euo pipefail

DIST="${1:-web/dist}"
ASSETS="${DIST}/assets"

if [ ! -d "${ASSETS}" ]; then
  echo "✗ ${ASSETS} not found — build the dashboard first: (cd web && npm run build)" >&2
  exit 2
fi

live_marker="JNPA_DATA_MODE:live"
mock_marker="JNPA_DATA_MODE:mock"
mock_sentinel="JNPA_MOCK_ADAPTER_PRESENT_DO_NOT_SHIP"

fail=0

if grep -rqs "${mock_marker}" "${ASSETS}"; then
  echo "✗ bundle was built in MOCK mode (found ${mock_marker})" >&2
  fail=1
fi

if grep -rqs "${mock_sentinel}" "${ASSETS}"; then
  echo "✗ MockAdapter is linked into the bundle (found ${mock_sentinel}) — not tree-shaken" >&2
  fail=1
fi

if ! grep -rqs "${live_marker}" "${ASSETS}"; then
  echo "✗ live build marker (${live_marker}) is missing from the bundle" >&2
  fail=1
fi

if [ "${fail}" -ne 0 ]; then
  echo "✗ FAIL: ${DIST} is NOT a shippable live bundle." >&2
  exit 1
fi

echo "✓ OK: ${DIST} is a LIVE bundle — live marker present, MockAdapter tree-shaken out."
