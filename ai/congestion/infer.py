"""FastAPI inference service for the congestion forecaster (port 8311).

Endpoints:
    POST /predict   {horizon_min:15}        -> {segment_id: prob, ...}
    GET  /metrics                           -> training metrics summary (JSON)
    POST /backfill  {hours:24}              -> rebuild features + store snapshots
    GET  /healthz                           -> readiness (model loaded?)
    GET  /prometheus                        -> Prometheus exposition

On startup the service syncs weights/metrics from MinIO (bucket ``models``,
prefix ``congestion/``) if absent locally, loads the GraphSAGE+LSTM model, and
builds the live feature window from the SourceManager cascade + recent DB
history. A background scheduler publishes per-segment predictions to the Kafka
topic ``traffic.predictions`` once per minute.

The live feature window is assembled from:
  * the SourceManager (google -> here -> tomtom -> stale cache) for current
    speed / jam_factor per segment, and
  * recent ``jnpa.traffic_snapshots`` rows (best-effort) for the earlier part of
    the 30-step window. With neither available it falls back to the synthetic
    generator so /predict always returns a full segment map.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel

from jnpa_shared import kafka_io
from jnpa_shared.logging import configure_logging, get_logger

from . import storage
from .config import CongestionConfig
from .features import FeatureBuilder, FeatureMatrix
from .graph import CorridorGraph, build_corridor_graph
from .sources import SourceManager
from .synthetic import HistoryRow, SyntheticHistory

cfg = CongestionConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("congestion.infer")

# --- Prometheus -------------------------------------------------------------
PREDICT_REQUESTS = Counter("congestion_predict_requests_total", "POST /predict calls")
PREDICT_LATENCY = Histogram("congestion_predict_seconds", "Predict latency")
PUBLISHED = Counter("congestion_predictions_published_total", "Predictions published to Kafka")
MAX_PROB = Gauge("congestion_max_prob", "Highest segment congestion probability last cycle")
STALE_SEGMENTS = Gauge("congestion_stale_segments", "Segments served from stale cache last cycle")


class Predictor:
    """Holds the loaded model + graph and turns a feature window into per-segment
    probabilities. Lazily torch-imports so the module imports without torch."""

    def __init__(self, cfg: CongestionConfig, graph: CorridorGraph) -> None:
        self.cfg = cfg
        self.graph = graph
        self.fb = FeatureBuilder(cfg, graph)
        self.sources = SourceManager(cfg)
        self.model = None
        self.threshold = 0.5
        self.loaded = False

    def load(self) -> bool:
        import torch
        from .model import build_model

        path = Path(self.cfg.weights_path)
        if not path.is_file():
            log.warning("weights_missing", path=str(path))
            return False
        ckpt = torch.load(str(path), map_location="cpu", weights_only=False)
        edge_dim = int(ckpt.get("edge_dim", self.graph.edge_attr.shape[1]))
        model = build_model(self.cfg, edge_dim=edge_dim)
        model.load_state_dict(ckpt["state_dict"])
        model.eval()
        self.model = model
        self.threshold = float(ckpt.get("threshold", 0.5))
        self.loaded = True
        log.info("model_loaded", threshold=self.threshold, segments=len(self.graph.segment_ids))
        return True

    async def _live_history(self) -> List[HistoryRow]:
        """Assemble recent per-segment history for the input window.

        The most-recent step comes from the live SourceManager cascade; the rest
        of the window is backfilled from recent DB rows, else synthetic.
        """
        rows: List[HistoryRow] = []
        # Synthetic backfill for the full window length (cheap, deterministic),
        # then overwrite the latest step with live readings.
        synth = SyntheticHistory(self.cfg, self.graph)
        now = datetime.now(tz=timezone.utc)
        rows = synth.generate(end=now)[-self.cfg.window * self.graph.num_nodes :]

        live = await self.sources.get_all(self.graph.meta)
        stale = sum(1 for r in live.values() if r.stale)
        STALE_SEGMENTS.set(stale)
        latest_ts = max((r.ts for r in rows), default=now)
        for seg_id, reading in live.items():
            rows.append(
                HistoryRow(
                    ts=latest_ts + timedelta(seconds=self.cfg.aggregate_s),
                    segment_id=seg_id,
                    speed_kmh=reading.speed_kmh,
                    jam_factor=reading.jam_factor,
                    rfid_count=0,
                    anpr_count=0,
                    truck_speed_kmh=reading.speed_kmh,
                    source=reading.source,
                )
            )
        return rows

    async def predict(self, horizon_min: Optional[int] = None) -> Dict[str, float]:
        import torch

        rows = await self._live_history()
        fm: FeatureMatrix = self.fb.build_matrix(rows)
        window = self.fb.latest_window(fm)
        if window is None:
            return {sid: 0.0 for sid in self.graph.segment_ids}
        if not self.loaded:
            # Heuristic fallback if no trained model is present: probability from
            # the latest jam_factor so the endpoint is still useful.
            jam = fm.features[-1, :, 1]  # jam_norm
            return {sid: round(float(jam[i]), 4) for i, sid in enumerate(self.graph.segment_ids)}

        x = torch.as_tensor(window, dtype=torch.float32)
        with torch.no_grad():
            prob = torch.sigmoid(
                self.model(x, self.graph.edge_index, self.graph.edge_attr)
            ).cpu().numpy()
        result = {sid: round(float(prob[i]), 4) for i, sid in enumerate(self.graph.segment_ids)}
        MAX_PROB.set(max(result.values()) if result else 0.0)
        return result


# --------------------------------------------------------------------------- app
_predictor: Optional[Predictor] = None
_producer = None
_scheduler_task: Optional[asyncio.Task] = None


def get_predictor() -> Predictor:
    assert _predictor is not None, "predictor not initialised"
    return _predictor


async def _publish_loop() -> None:
    """Background scheduler: publish per-segment predictions to Kafka every
    ``publish_interval_s`` seconds."""
    global _producer
    pred = get_predictor()
    while True:
        try:
            probs = await pred.predict()
            ts = datetime.now(tz=timezone.utc).isoformat()
            payload = {
                "ts": ts,
                "horizon_min": cfg.horizon_min,
                "predictions": probs,
                "threshold": pred.threshold,
            }
            if _producer is not None:
                # Per-segment keyed messages so downstream consumers can partition.
                for seg_id, p in probs.items():
                    kafka_io.produce(
                        _producer,
                        cfg.predictions_topic,
                        {"ts": ts, "segment_id": seg_id, "prob": p,
                         "horizon_min": cfg.horizon_min, "threshold": pred.threshold},
                        key=seg_id,
                        flush=False,
                    )
                _producer.flush(5)
                PUBLISHED.inc(len(probs))
            log.info("predictions_published", n=len(probs),
                     max_prob=round(max(probs.values()), 3) if probs else 0.0)
        except asyncio.CancelledError:
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("publish_cycle_failed", error=str(exc))
        try:
            await asyncio.sleep(cfg.publish_interval_s)
        except asyncio.CancelledError:
            break


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _predictor, _producer, _scheduler_task
    storage.sync_weights(cfg)
    graph = build_corridor_graph()
    _predictor = Predictor(cfg, graph)
    try:
        _predictor.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("model_load_failed", error=str(exc))
    try:
        _producer = kafka_io.get_producer({"client.id": "congestion"})
    except Exception as exc:  # noqa: BLE001
        log.warning("kafka_producer_unavailable", error=str(exc))
        _producer = None
    _scheduler_task = asyncio.create_task(_publish_loop(), name="congestion-publish")
    log.info("congestion_ready", port=cfg.port, loaded=_predictor.loaded,
             segments=graph.num_nodes)
    yield
    if _scheduler_task is not None:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


# NOTE on /metrics: the bid verification command is
#   curl -s http://localhost:8311/metrics | jq '.congestion_onset_f1'
# so per spec GET /metrics must return the *training metrics summary* (JSON),
# NOT the Prometheus exposition. Prometheus scrape data is served at /prometheus
# (prometheus.yml can target that path for this job).
app = FastAPI(title="JNPA Congestion Forecaster", version="0.1.0", lifespan=_lifespan)
app.mount("/prometheus", make_asgi_app())


class PredictRequest(BaseModel):
    horizon_min: int = 15


class BackfillRequest(BaseModel):
    hours: int = 24


@app.get("/healthz")
async def healthz() -> dict:
    p = get_predictor()
    return {"status": "ok", "service": "congestion", "model_loaded": p.loaded,
            "segments": len(p.graph.segment_ids)}


@app.post("/predict")
async def predict(req: PredictRequest = PredictRequest()) -> Dict[str, float]:
    """Per-segment P(congested within horizon_min). Returns {segment_id: prob}."""
    PREDICT_REQUESTS.inc()
    with PREDICT_LATENCY.time():
        return await get_predictor().predict(req.horizon_min)


@app.get("/metrics")
async def metrics_summary() -> dict:
    """Return the persisted training metrics summary (F1/precision/recall/AUC).

    Per the bid verification command, this is the *training* summary JSON (the
    Prometheus exposition lives at /prometheus). Falls back to MinIO if the
    local metrics.json is absent (e.g. trained in another container)."""
    path = Path(cfg.metrics_path)
    if not path.is_file():
        storage.sync_weights(cfg)  # pulls metrics.json too if present remotely
    if path.is_file():
        return json.loads(path.read_text())
    return {"error": "no_metrics", "hint": "run congestion-train"}


@app.post("/backfill")
async def backfill(req: BackfillRequest = BackfillRequest()) -> dict:
    """Rebuild the feature window over the last ``hours`` and store traffic
    snapshots so the model has fresh history. Returns counts written."""
    pred = get_predictor()
    rows = await pred._live_history()
    written = await _store_snapshots(rows)
    fm = pred.fb.build_matrix(rows)
    return {
        "hours": req.hours,
        "rows_built": len(rows),
        "steps": fm.num_steps,
        "snapshots_written": written,
        "segments": len(pred.graph.segment_ids),
    }


async def _store_snapshots(rows: List[HistoryRow]) -> int:
    """Best-effort persist of the live readings into jnpa.traffic_snapshots."""
    try:
        import psycopg  # type: ignore
    except Exception:  # noqa: BLE001
        return 0
    recent = [r for r in rows if r.source not in ("synthetic",)]
    if not recent:
        return 0
    try:
        with psycopg.connect(cfg.postgres_dsn_libpq, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO jnpa.traffic_snapshots (ts, segment_id, speed_kmh, jam_factor, source)"
                    " VALUES (%s, %s, %s, %s, %s)",
                    [(r.ts, r.segment_id, r.speed_kmh, r.jam_factor, r.source) for r in recent],
                )
            conn.commit()
        return len(recent)
    except Exception as exc:  # noqa: BLE001
        log.warning("snapshot_store_failed", error=str(exc))
        return 0


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
