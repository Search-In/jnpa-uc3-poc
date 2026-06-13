# Behavioural Anomaly Detector — UC-III Sub-Criterion 2C

Hybrid behavioural anomaly detection over the NH-348 corridor:
**ByteTrack** (vehicle tracking) **+ a rule engine** (wrong-way, abandoned,
illegal-parking, route-deviation) **+ a 1D-conv trajectory autoencoder** that
catches behaviours the rules can't enumerate (e.g. slow looping). Alerts land in
`jnpa.alerts` and the Kafka `alerts` topic, each with the offending frame saved
to MinIO as evidence.

```
ai/anomaly/
├── app.py                FastAPI on :8321 (/alerts/recent, /train_ae, /health, /metrics)
├── engine.py             runs rules + AE per track, dedupes, attaches evidence, emits
├── types.py              Track / TrackPoint + bearing helpers (shared currency)
├── motion.py             stationarity / dwell primitive (abandoned + parking)
├── cameras.py            per-camera ground geo for image→latlon projection
├── synthetic.py          normal-track corpus + named scenario builders (tests)
├── train.py              AE training pipeline (telemetry + synthetic) + CLI
├── sink.py               Alert -> jnpa.alerts (psycopg) + Kafka "alerts"
├── evidence.py           save offending frame -> MinIO evidence/{alert_id}.jpg
├── storage.py            MinIO persistence (models + evidence buckets)
├── route_lookup.py       assigned-route fetch from truck-sim /devices/{id}/route
├── workers.py            frame-bus tracker loop + Kafka telemetry loop
├── metrics.py            Prometheus counters/gauges
├── track/
│   └── bytetrack.py      ByteTrack (supervision wrapper) + YOLOv8 detector
└── autoencoder/
    ├── features.py       per-track speed + sin/cos-heading feature matrix
    └── model.py          symmetric 1D-conv AE + 99th-pct threshold + score
```

## Detection paths

The detector ingests tracks from two sources, both producing the same `Track`
type so the rules and AE are source-agnostic:

* **ByteTrack over the frame bus** — `ingest/anpr` mirrors sampled jpeg frames to
  Redis Streams `frames.{camera_id}` (5 fps, trimmed to the last 600). The
  frame-tracker worker tails those, runs YOLOv8 → `sv.ByteTrack`, and projects
  each bbox-centre to a ground `(lat, lon)` via `cameras.project`.
* **Trucking-app telemetry** — the telemetry worker tails the Kafka
  `truck.telemetry` topic, maintaining a per-device GPS track (real lat/lon,
  speed, heading), and fetches the device's assigned route for route-deviation.

ByteTrack needs `supervision` + `ultralytics` + `torch`. If they're absent the
service runs **rules + AE on the telemetry path** and logs the tracker inactive
(the same graceful-degradation pattern as `ai/anpr` and `ai/congestion`).

## Rules

| Rule | Trigger | Alert | Severity |
|------|---------|-------|----------|
| `wrongway` | heading diverges from the camera's allowed bearing by **>120°** for **>2 s** | `WRONG_WAY` | critical |
| `abandoned` | stationary **outside** every no-park zone for **>120 s** | `ABANDONED` | warning |
| `parking` | stationary **inside** a `NO_PARK_ZONES` polygon for **>300 s** | `ILLEGAL_PARKING` | escalates |
| `route_deviation` | cosine distance **>0.4** OR off-route **>800 m**, sustained **>90 s** | `ROUTE_DEVIATION` | warning |

Illegal-parking escalates by dwell duration: **WARNING** @5 min, **CRITICAL**
@15 min, **REPORT_TO_POLICE** @30 min (carried in `payload.escalation`).
Abandoned and illegal-parking are mutually exclusive (outside-zone vs in-zone),
so a stationary vehicle raises exactly one of them.

The six no-parking polygons live in `jnpa_shared.corridor.NO_PARK_ZONES` (gate
aprons, junction throats, a flyover ramp, a weighbridge approach) with
`point_in_polygon` / `zone_for_point` helpers.

## Autoencoder

A small symmetric 1D-conv autoencoder over per-track trajectory features —
the **speed series** plus the **sin/cos of heading** (the dwell pattern is
implicit in the speed channel). Each track is resampled to a fixed `ae_seq_len`
(64) so one model handles any duration and reconstruction errors are comparable.

Trained on *normal* corridor trajectories (recent `truck.telemetry`, blended
with a synthetic normal corpus so it never starves on a fresh stack). The
anomaly threshold is the **99th percentile** of the training reconstruction
error. A track whose error exceeds that is flagged `ANOMALOUS_TRAJECTORY` — this
catches odd behaviours (slow looping, weaving) the rule set doesn't enumerate.

## Evidence pipeline

On every alert the detector saves the offending frame to MinIO under
`evidence/{alert_id}.jpg` and attaches the URL to `alert.payload.evidence_url`.
The frame is the exact one ByteTrack was processing, or — for telemetry-sourced
alerts — the most-recent frame on that camera's bus stream. This is required for
the **TFC-2 wrong-way scenario** in Prompt 8.

## API (port 8321)

```
GET  /alerts/recent?since=PT1H   -> list[Alert]   (ISO-8601 duration or timestamp)
GET  /alerts/recent?kind=WRONG_WAY&since=PT6H
POST /train_ae   {"days": 7}     -> train the AE on the last N days of tracks
GET  /health                     -> readiness (AE loaded, workers active)
GET  /metrics                    -> Prometheus exposition (mounted at /metrics/)
```

`since` accepts an ISO-8601 duration relative to now (`PT1H`, `P1D`, `PT90M`),
an absolute ISO timestamp, or nothing (defaults to the last hour).

## Verify

```bash
# Bid verification command:
curl -s 'http://localhost:8321/alerts/recent?since=PT1H' | jq 'length'

# Health + a per-kind breakdown:
make anomaly-verify

# Train the AE in-process (needs torch), no stack required:
make anomaly-train
```

## Tests

`tests/test_anomaly.py` injects synthetic wrong-way / abandoned / illegal-park
tracks and asserts each produces **exactly one** alert of the correct kind, plus
route-deviation, the no-park-zone geometry, and (torch-gated) an end-to-end AE
train where a looping trajectory scores above the normal-track threshold. The
pure-logic tests need no infra and run on a bare CPU host.
```bash
pytest tests/test_anomaly.py -v
```
