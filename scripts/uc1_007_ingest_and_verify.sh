#!/usr/bin/env bash
# UC1-007 — ingest NLP Marine (via UC1-002) then re-verify PCS parse counts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BASE="${UC1_CORPUS_BASE:-${CORPUS_BASE:-}}"
NLP="${MARINE_DATA_DIR:-}"
DSN="${POSTGRES_DSN:-postgresql+asyncpg://postgres:jnpa_pw@127.0.0.1:5433/jnpa_v3_local}"

if [[ -z "$NLP" && -n "$BASE" ]]; then
  NLP="$BASE/1-NLP Marine"
fi
if [[ -z "$NLP" && -d "$ROOT/../jnpa_poc_1/data/NLP Marine" ]]; then
  NLP="$ROOT/../jnpa_poc_1/data/NLP Marine"
fi
if [[ -z "$BASE" && -n "$NLP" ]]; then
  BASE="$(dirname "$NLP")"
fi

if [[ -z "$NLP" || ! -d "$NLP" ]]; then
  cat <<EOF
UC1-007: 1-NLP Marine corpus not found.

Set one of:
  export UC1_CORPUS_BASE="/path/to/Digital Twin Data Corpus - Updated/Data"
  export MARINE_DATA_DIR="\$UC1_CORPUS_BASE/1-NLP Marine"
Or copy/symlink the folder to:
  $ROOT/../jnpa_poc_1/data/NLP Marine
EOF
  exit 2
fi

export POSTGRES_DSN="$DSN"
export MARINE_DATA_DIR="$NLP"

echo "== parse verify =="
.venv/bin/python scripts/verify_pcs_parse_counts.py --nlp "$NLP"

if [[ -n "$BASE" && -d "$BASE" ]]; then
  echo "== ingest (UC1-002) =="
  .venv/bin/python scripts/ingest_uc1_corpus.py --base "$BASE"
  echo "== verify + DB =="
  .venv/bin/python scripts/verify_pcs_parse_counts.py --nlp "$NLP" --dsn "$DSN"
else
  echo "Skipping ingest (UC1_CORPUS_BASE parent not set); parse-only verify done."
fi
