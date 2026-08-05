# eir-ocr — EIR / gate-slip OCR ingest (Tesseract)

FastAPI microservice that OCRs WhatsApp / phone photos of terminal EIR slips
into structured fields. Ports the preprocessing + confusion-folding approach
from the workspace `docs/ocr_gate_docs.py` verifier, and adds real field
extractors (container / plate / EIR no / weight / …).

| Route | Role |
| --- | --- |
| `POST /infer` | multipart image → `{raw_text, fields, confidence, …}` |
| `POST /infer_batch` | multiple files |
| `GET /healthz` | tesseract readiness |
| `GET /metrics` | Prometheus |

Default port **8210**. Gateway/UI wiring is intentionally out of scope for this
slice — call the service directly, or point a future `document_ocr` hook at
`http://eir-ocr:8210/infer`.

## Local run

```bash
# deps
brew install tesseract          # macOS
pip install -e "ingest/eir_ocr"

# API
PYTHONPATH=ingest eir-ocr
# or: PYTHONPATH=ingest uvicorn eir_ocr.app:app --port 8210 --reload

curl -s -F "file=@../../EIR/WhatsApp\ Image\ 2026-06-12\ at\ 19.36.11\ \(1\).jpeg" \
     -F "doc_type=EIR" \
     http://localhost:8210/infer | jq .
```

## Batch verify (the 4 local EIR photos)

```bash
PYTHONPATH=ingest eir-ocr-verify \
  --images-dir ../../EIR \
  --expected ingest/eir_ocr/fixtures/eir_expected.json \
  --out /tmp/eir_ocr_report.md \
  --json /tmp/eir_ocr_report.json
```

Self-test gates key fields (ContainerNo / LICNo / EIRNo where present; blank
ContainerNo on the DP World slip). Exit code 1 if a gate fails.

## Unknown / future slip fields

Known fields live in `fields` (see `EIR_FIELDS` + `LABEL_MAP` in `extract.py`).

If a future photo prints a **new** `Label: value` that we have not mapped yet,
it is **not dropped** — it appears under `extras` in the API / verify report:

```json
{
  "fields": { "ContainerNo": { "value": "…", "conf": 0.9 }, "…": "…" },
  "extras": { "ImcoUnNo": { "value": "1234", "conf": 0.55, "evidence": "IMCO/UN NO: 1234" } }
}
```

When the same label shows up often, promote it into `LABEL_MAP` / `EIR_FIELDS`
(and add a cleaner + fixture expectation). Until then, downstream can still
read `extras` so nothing on the paper is silently lost.

## Efficiency

1. Grayscale → 2× upscale → autocontrast (+ binarise) × PSM 6/4
2. Early-exit when ≥2 of {ContainerNo, LICNo, EIRNo} are extracted
3. In-process SHA-256 LRU cache (`EIR_OCR_CACHE_SIZE`, default 64)

## Env

| Var | Default | Meaning |
| --- | --- | --- |
| `PORT` / `EIR_OCR_PORT` | 8210 | HTTP port |
| `EIR_OCR_CACHE_SIZE` | 64 | LRU entries |
| `EIR_OCR_EARLY_EXIT_MIN` | 2 | high-value fields required to stop |
| `EIR_IMAGES_DIR` | (auto) | default batch images dir |
| `LOG_LEVEL` | INFO | |

## Compose

```bash
docker compose up -d --build eir-ocr
curl -s http://localhost:8210/healthz | jq .
```

## Tests

```bash
PYTHONPATH=ingest:tests pytest tests/test_eir_ocr.py -q
```

Extractor/normalize unit tests need no tesseract. Marked integration tests skip
unless the binary is present.
