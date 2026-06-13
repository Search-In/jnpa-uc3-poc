# anpr-ai — ANPR + OCR inference service

ANPR + OCR inference for the JNPA UC-III PoC (Sub-Criterion 2A), with formal
accuracy measurement on a held-out benchmark slice. The bid commits to **≥ 95 %
OCR accuracy** under port operating conditions (dust, fog, night).

## Pipeline

```
frame ─▶ YOLOv8 plate detector ─▶ PaddleOCR (PP-OCRv4, Indian fine-tune) ─▶ post-processor ─▶ {plate, conf, bbox}
        (src/anpr/detect.py)       (src/anpr/ocr.py)                         (src/anpr/postprocess.py)
```

- **detect.py** — YOLOv8 license-plate detector. We do **not** retrain from
  scratch: it loads the publicly-released `license_plate_detector.pt` from
  [computervisioneng/automatic-number-plate-recognition-python-yolov8](https://github.com/computervisioneng/automatic-number-plate-recognition-python-yolov8).
  The weights file is **hash-verified on startup** (`ANPR_YOLO_SHA256`).
- **ocr.py** — PaddleOCR PP-OCRv4 recogniser with a custom
  `rec_char_dict_path` → `resources/indian_plate_chars.txt` (A–Z 0–9) and the
  fine-tuned adapter under `resources/rec_indian/`.
- **postprocess.py** — applies the Indian plate grammar
  `^([A-Z]{2})[ -]?([0-9]{1,2})[ -]?([A-Z]{1,3})[ -]?([0-9]{4})$` plus the
  BH-series `^([0-9]{2})BH([0-9]{4})([A-Z]{1,2})$`, a state-code whitelist
  (`resources/state_codes.txt`), and a confusion-fixer
  `{O→0, I→1, S→5, B→8, Z→2}` applied **only** on positions the regex says must
  be digits (and the inverse on letter positions).

### CPU graceful degradation

PaddleOCR + paddlepaddle are heavyweight and GPU-oriented. The container ships
them (`[ocr]` extra), so the **real stack runs in-container and meets the
≥ 95 % target**. If paddle / the YOLO weights cannot load (e.g. a bare CPU host
with neither installed), the service degrades to a **classical contrast-based
detector + a deterministic template-matching OCR** so `/infer` and `/eval` keep
answering — the same no-ML fallback contract the rest of the PoC honours
(`ingest/anpr`, the Vahan simulator, synthetic clips).

The `/eval` response and `metrics.json` carry an **`engine`** field
(`paddle+yolo` or `fallback`) and a `degraded` flag, so a reported number is
never misread: the fallback reports its real, lower accuracy honestly rather
than gaming the gate.

## API (port 8301)

| Method | Path           | Body                              | Returns |
| ------ | -------------- | --------------------------------- | ------- |
| POST   | `/infer`       | multipart `image`                 | `{plate, conf, bbox, valid, series, raw_ocr, fixes, degraded}` |
| POST   | `/infer_batch` | JSON `{"images": ["<b64>", ...]}` | `{count, results:[...]}` |
| GET    | `/eval`        | optional `?n=`                    | metrics + `OCR_TARGET_MET` |
| GET    | `/healthz`     | —                                 | readiness + weights hash |
| GET    | `/metrics`     | —                                 | Prometheus |

`/infer` accepts a full camera frame **or** an already-cropped plate — the
detector finds the ROI either way. `ingest/anpr` POSTs plate crops here when
`DRY_RUN=false`.

## Evaluation suite (`eval/bench.py`)

Three slices, scored against the held-out 15 % tail of the shared Vahan plate
fixture (`data/fixtures/known_plates.json`); each plate is rendered into a
synthetic camera scene (`src/anpr/plategen.py`) and degraded
(`src/anpr/degradation.py`):

| Slice | Condition          | Target |
| ----- | ------------------ | ------ |
| (a)   | Clean              | char accuracy ≥ 97 % (CER < 3 %) **and** exact-match ≥ 95 % |
| (b)   | Dust + haze        | exact-match ≥ 92 % |
| (c)   | Night / low-light  | exact-match ≥ 90 % |

`metrics.json` carries per-slice CER/WER/exact-match + per-slice detection
recall/IoU. The runner prints a final line:

```
OCR_TARGET_MET=true|false   # true requires combined weighted accuracy ≥ 95.0 %
```

Combined weighted accuracy weights `clean:0.5, dust_haze:0.25, night:0.25`.

```bash
# Offline, in-process (no stack needed):
python ai/anpr/eval/bench.py --n 200
# Against the running container:
curl -s http://localhost:8301/eval | jq .
```

## One-time fine-tuning (`src/anpr/finetune.py`)

Pulls a public Indian-plate dataset (first that responds, in order):

1. `kaggle:sarthakvajpayee/indian-vehicle-dataset`
2. `github:sanchit2843/Indian_LPR`
3. `github:Rishit-dagli/Vehicle-License-Plate-Detection`

then fine-tunes the PP-OCRv4 recogniser for **30 epochs, backbone frozen for
the first 10**, exporting the adapter to `resources/rec_indian/`.

- **Expected GPU time: ~25 min on a single T4.**
- **CPU fallback:** there is no practical CPU training path — a pre-baked
  adapter is shipped in the repo (or stock PP-OCRv4 + the char-dict +
  post-processor suffice for the PoC). Run `python -m anpr.finetune --train`
  on a GPU box to regenerate.

```bash
python -m anpr.finetune --status         # report adapter / dataset state
python -m anpr.finetune --download-only  # probe + print the dataset fetch cmd
python -m anpr.finetune --train          # GPU box: full fine-tune
```

## Weights persistence (MinIO)

On startup the service reconciles the YOLO weights with the MinIO `models`
bucket (`src/anpr/storage.py`): uploads a local-only file, or pulls one that
exists only in MinIO. Best-effort — MinIO being down never crashes the API.

## Verification

```bash
curl -s -F "image=@./resources/sample_plate.jpg" http://localhost:8301/infer | jq .
curl -s http://localhost:8301/eval | jq .   # OCR_TARGET_MET=true on the real stack
```

See the repo-root `README.md` for full bring-up instructions.
