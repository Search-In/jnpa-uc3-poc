"""FastAPI service for the behavioural anomaly detector (port 8321).

Endpoints (per the bid spec):
    GET  /alerts/recent?since=PT1H   -> list[Alert]   (ISO-8601 duration or ts)
    POST /train_ae                   -> train the AE on the last N days of tracks
    GET  /health                     -> readiness
    GET  /metrics                    -> Prometheus exposition (mounted)

Background workers (started in the lifespan):
    * frame-bus tracker loop — consumes ``frames.{camera_id}`` Redis streams,
      runs ByteTrack + the rules + the AE, emits alerts with frame evidence.
      Inactive (logged) if supervision/ultralytics/torch are not installed.
    * telemetry loop — consumes the Kafka ``truck.telemetry`` topic, maintains a
      per-device GPS track, and runs the rules (incl. route-deviation against the
      assigned route) + the AE on it. This path needs no heavy ML deps, so the
      detector is useful out of the box even without ByteTrack.

On startup it syncs AE weights from MinIO (bucket ``models`` prefix ``anomaly/``)
and loads them if present; otherwise the AE stays inactive until /train_ae runs.
"""
from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import FastAPI, Query
from pydantic import BaseModel

from jnpa_shared.logging import configure_logging, get_logger

from .autoencoder.model import TrajectoryAutoencoder
from .config import AnomalyConfig
from .engine import AnomalyEngine
from .evidence import EvidenceWriter
from .metrics import (
    AE_THRESHOLD,
    AE_TRAININGS,
    metrics_asgi_app,
)
from .route_lookup import RouteCache
from .sink import AlertSink
from .train import train_autoencoder
from .workers import FrameTrackerWorker, TelemetryWorker

cfg = AnomalyConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("anomaly.app")

# Shared singletons populated in the lifespan.
_engine: Optional[AnomalyEngine] = None
_sink: Optional[AlertSink] = None
_evidence: Optional[EvidenceWriter] = None
_ae: Optional[TrajectoryAutoencoder] = None
_workers: List = []
_worker_tasks: List[asyncio.Task] = []


def get_engine() -> AnomalyEngine:
    assert _engine is not None, "engine not initialised"
    return _engine


# --------------------------------------------------------------------------- since parsing
_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def parse_since(since: Optional[str]) -> datetime:
    """Parse the ``since`` query param into an absolute UTC datetime.

    Accepts an ISO-8601 duration relative to now (``PT1H`` = last hour, the bid
    verification form), an absolute ISO-8601 timestamp, or None (defaults to the
    last hour).
    """
    now = datetime.now(tz=timezone.utc)
    if not since:
        return now - timedelta(hours=1)
    m = _ISO_DURATION.match(since.strip())
    if m and any(m.group(g) for g in ("days", "hours", "minutes", "seconds")):
        gd = {k: int(v) for k, v in m.groupdict(default="0").items()}
        delta = timedelta(days=gd["days"], hours=gd["hours"],
                          minutes=gd["minutes"], seconds=gd["seconds"])
        return now - delta
    try:
        ts = datetime.fromisoformat(since.replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return now - timedelta(hours=1)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _engine, _sink, _evidence, _ae, _workers, _worker_tasks

    # AE: sync from MinIO + load if present (else inactive until /train_ae).
    storage_sync()
    _ae = TrajectoryAutoencoder(cfg)
    try:
        if _ae.load():
            AE_THRESHOLD.set(_ae.threshold)
            log.info("ae_loaded", threshold=_ae.threshold)
        else:
            log.info("ae_inactive", reason="no_weights_or_torch")
    except Exception as exc:  # noqa: BLE001
        log.warning("ae_load_failed", error=str(exc))

    _sink = AlertSink(cfg)
    _sink.start()
    _evidence = EvidenceWriter(cfg)
    _engine = AnomalyEngine(cfg, sink=_sink, evidence=_evidence, autoencoder=_ae)

    # Background workers.
    route_cache = RouteCache(cfg)
    _workers = [
        FrameTrackerWorker(cfg, _engine),
        TelemetryWorker(cfg, _engine, route_cache),
    ]
    _worker_tasks = [asyncio.create_task(w.run(), name=w.name) for w in _workers]

    log.info("anomaly_ready", port=cfg.port, ae_loaded=_ae.loaded,
             workers=[w.name for w in _workers])
    try:
        yield
    finally:
        for w in _workers:
            w.stop()
        for t in _worker_tasks:
            t.cancel()
        await asyncio.gather(*_worker_tasks, return_exceptions=True)
        if _sink is not None:
            _sink.close()
        if _evidence is not None:
            _evidence.close()


def storage_sync() -> None:
    from . import storage as _storage
    try:
        _storage.sync_weights(cfg)
    except Exception as exc:  # noqa: BLE001
        log.warning("ae_sync_failed", error=str(exc))


app = FastAPI(title="JNPA Behavioural Anomaly Detector", version="0.1.0", lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


# --------------------------------------------------------------------------- models
class TrainRequest(BaseModel):
    days: Optional[int] = None   # defaults to cfg.ae_train_days


# --------------------------------------------------------------------------- endpoints
@app.get("/health")
async def health() -> dict:
    eng = _engine
    return {
        "status": "ok",
        "service": "anomaly",
        "ae_loaded": bool(_ae and _ae.loaded),
        "ae_threshold": (_ae.threshold if (_ae and _ae.loaded) else None),
        "workers": [
            {"name": w.name, "active": w.active} for w in _workers
        ],
    }


@app.get("/alerts/recent")
async def alerts_recent(
    since: Optional[str] = Query(default=None, description="ISO-8601 duration (PT1H) or timestamp"),
    kind: Optional[str] = Query(default=None, description="filter to one alert kind"),
    limit: int = Query(default=1000, ge=1, le=10000),
) -> List[dict]:
    """Return alerts raised since ``since`` (default: last hour), newest first."""
    assert _sink is not None
    start = parse_since(since)
    kinds = [kind] if kind else None
    alerts = await asyncio.to_thread(_sink.recent, start, kinds, limit)
    return [a.model_dump(mode="json") for a in alerts]


@app.post("/train_ae")
async def train_ae(req: TrainRequest = TrainRequest()) -> dict:
    """Train the trajectory AE on the last N days of tracks; hot-swap on success."""
    global _ae
    result = await asyncio.to_thread(train_autoencoder, cfg, req.days)
    status = result.get("status")
    if status == "ok":
        # Reload the freshly-trained weights into the engine.
        new_ae = TrajectoryAutoencoder(cfg)
        if new_ae.load():
            _ae = new_ae
            if _engine is not None:
                _engine.ae = new_ae
            AE_THRESHOLD.set(new_ae.threshold)
        AE_TRAININGS.labels(result="ok").inc()
    elif status == "skipped":
        AE_TRAININGS.labels(result="skipped").inc()
    else:
        AE_TRAININGS.labels(result="error").inc()
    return result


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
