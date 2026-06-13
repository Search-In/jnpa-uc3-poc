# anpr-ingest

ANPR ingestion service for the JNPA UC-III PoC.

Replays MP4 clips from `/data/clips/` as virtual RTSP feeds, runs a YOLOv8n
vehicle detector, crops candidate plates, and emits `AnprRead` events to the
Kafka topic `anpr.reads`. Each frame is tagged with current weather
(fog / rain / dust / clear) pulled from OpenWeatherMap every 10 minutes.

- `DRY_RUN=true` (default): emit raw crops only; do not call the AI ANPR
  service (built in Prompt 3.1).
- With zero clips present, the service stays alive and emits a `no_feed`
  health event every 5 s.
- Prometheus metrics on container port `9101` (`/metrics`).

See the repo root `README.md` for full bring-up instructions.
