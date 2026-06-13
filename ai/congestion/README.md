# Congestion Forecaster — UC-III Sub-Criterion 2B

GraphSAGE encoder over the NH-348 corridor graph feeding a 2-layer LSTM that
predicts **P(congested in the next 15 min)** per corridor segment. Target:
`congestion_onset_f1 ≥ 0.85`, `precision ≥ 0.80`, `recall ≥ 0.80`.

```
ai/congestion/
├── model.py        GraphSAGE encoder + 2-layer LSTM (PyTorch)
├── graph.py        builds the PyG corridor graph from jnpa_shared.corridor
├── features.py     rolling 30×60-s feature windows + onset labels
├── synthetic.py    14-day "fast-forward" bootstrap history (commute-aware)
├── train.py        class-weighted BCE training; F1/P/R/ROC-AUC on held-out 24 h
├── infer.py        FastAPI on :8311 (/predict, /metrics, /backfill)
├── metrics.py      numpy-only F1 / precision / recall / ROC-AUC
├── storage.py      MinIO persistence (bucket "models", prefix congestion/)
└── sources/        google.py · here.py · tomtom.py · SourceManager (cascade)
```

## Model

* **Nodes** = corridor segments (`SEG-00 …` from `jnpa_shared.corridor`).
* **Edges** = physical adjacency (i ↔ i+1) + self-loops; each edge carries
  `lane_count_norm` and a `signalised` flag that gate the GraphSAGE message.
* **Encoder** = 2-layer GraphSAGE (mean aggregator + edge gate) applied at every
  step of the 30-step window, vectorised over `(batch × time)` graphs.
* **Temporal** = a 2-layer LSTM shared across segments over the window; last
  hidden state → linear head → one logit per segment → sigmoid.
* **Input window** = 30 steps of 60-s aggregates (= 30 min of history).

Per-step node features (`features.FEATURE_NAMES`, F = 9): speed, jam factor,
RFID count, ANPR count, trucking-app median speed (all normalised), static lane
count + signalised flag, and a time-of-day sin/cos (IST commute clock).

A segment is **congested** when `jam_factor ≥ 6` *or* `speed ≤ 18 km/h`. The
label is genuine **onset**: positive if the segment congests within the horizon
*and was not already congested* at the window end (already-jammed segments are
masked out of scoring).

## Data

* **Bootstrap (synthetic).** `synthetic.py` runs 14 days of corridor commute
  physics forward (the "5× fast-forward" generator) — morning inbound peak
  (~09:00) loading the port end, evening outbound peak (~18:45) loading the
  junction end, a midday truck-shift bump, lighter weekends, signal/lane
  saturation, jam propagation to adjacent segments, and a handful of sharp
  incidents so there are real onset events to learn. Seeded → reproducible.
* **Real enrichment.** `train.py` best-effort joins recent
  `jnpa.traffic_snapshots` (overriding synthetic at matching `ts`/segment) when
  Postgres is reachable. (HERE tile-archive backfill is a one-time optional pull
  when a key with archive access is configured.)

## External traffic sources

`sources/SourceManager.get(seg)` tries **google → here → tomtom**, 1-second
timeout each, caches in Redis for 90 s, and on total failure returns the last
cached value marked `stale=true` (the Sub-Criterion 3 fallback foundation).
Without API keys each adapter returns a deterministic, commute-shaped synthetic
reading so the cascade and the prediction loop run end-to-end offline.

## Run it

```bash
# Train (host): writes artifacts + uploads to MinIO; exits non-zero if under target.
make congestion-train          # or: PYTHONPATH=ai:shared python -m congestion.train

# Serve (compose): the `congestion` service trains on first boot then serves :8311.
make up

# Verify (bid command):
curl -s -XPOST http://localhost:8311/predict -d '{"horizon_min":15}' \
  -H 'content-type: application/json' | jq '. | length'
curl -s http://localhost:8311/metrics | jq '.congestion_onset_f1'
```

Predictions are also published to the Kafka topic **`traffic.predictions`**
once per minute by a background scheduler (one keyed message per segment).

## Endpoints (`:8311`)

| Method | Path           | Body / Returns |
|--------|----------------|----------------|
| POST   | `/predict`     | `{horizon_min:15}` → `{segment_id: prob}` |
| GET    | `/metrics`     | training metrics summary (F1/P/R/ROC-AUC) |
| POST   | `/backfill`    | `{hours:24}` → rebuilds features, stores snapshots |
| GET    | `/healthz`     | readiness (model loaded?) |
| GET    | `/prometheus`  | Prometheus exposition |
