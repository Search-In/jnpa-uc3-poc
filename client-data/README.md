# client-data (local, gitignored)

Place the Digital Twin NLP Marine corpus here for UC1-007 / parser tests:

```text
../jnpa_poc_1/data/NLP Marine/
  Inbound_CALINF_BERMAN/
  Outbound_CALINV_BERALT/
  CALINF/
  BERMAN/
  VESPRO/
  VESARR/
  VESDEP/
```

Or set:

```bash
export MARINE_DATA_DIR="/path/to/…/1-NLP Marine"
# or the Data parent:
export UC1_CORPUS_BASE="/path/to/Digital Twin Data Corpus - Updated/Data"
```

Then:

```bash
.venv/bin/python scripts/verify_pcs_parse_counts.py
.venv/bin/python scripts/ingest_uc1_corpus.py --base "$UC1_CORPUS_BASE"
```
