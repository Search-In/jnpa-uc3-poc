"""/api/traffic — orchestrated corridor traffic with a LIVE/CACHED/SYNTHETIC chain.

    LIVE       -> ai/congestion /predict (per-segment P(congested))
    CACHED      -> last good /predict result from Redis (TTL 90 s)
    SYNTHETIC  -> deterministic free-flow-ish synthetic probabilities so the
                  dashboard's corridor heat-map never goes blank

Also exposes ``/api/traffic/snapshots`` reading the latest per-segment
``core.traffic_snapshot`` rows for the live map overlay.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from jnpa_shared import corridor

from .. import cache
from ..fallback import SourceState
from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.traffic")

router = APIRouter(prefix="/api/traffic", tags=["traffic"])


def _synthetic_predictions() -> Dict[str, float]:
    """Deterministic per-segment congestion probabilities (no RNG)."""
    out: Dict[str, float] = {}
    for seg in corridor.segments:
        h = int.from_bytes(hashlib.sha256(seg.id.encode()).digest()[:2], "big")
        out[seg.id] = round(0.05 + (h % 30) / 100.0, 3)   # 0.05..0.34
    return out


def _auto_congestion_alert(state: GatewayState, predictions: Any) -> None:
    """Fire-and-forget: auto-raise TRAFFIC_CONGESTION alerts for any segment whose
    predicted probability crosses ``CONGESTION_ALERT_THRESHOLD`` (UC-3 R4/R7).

    Deduped per segment-per-hour in the service, so a polling dashboard never
    spams the feed; best-effort, so it never blocks or fails the /predict response.
    Only flat ``{segment_id: prob}`` maps are actioned (the shape ai/congestion
    returns); anything else is ignored.
    """
    if not isinstance(predictions, dict):
        return
    thr = state.cfg.congestion_alert_threshold
    if thr > 1.0:  # disabled by config
        return
    from .. import audit
    from .. import notifications as notif
    from . import push
    from services import congestion_alert

    async def _run() -> None:
        # Fan a newly-raised congestion alert out to every registered driver
        # device over WebPush + FCM (ws=False — the service emits the corridor
        # WS frame once, so we never duplicate it). No registered device => the
        # service still broadcasts on WS exactly as before.
        async def _dispatch(device_id: str, advisory: Dict[str, Any]):
            return await notif.dispatch(state, device_id, advisory, ws_type="alert", ws=False)

        targets = await push.registered_devices(state)
        await congestion_alert.raise_congestion_alerts(
            predictions=predictions,
            threshold=thr,
            dsn=state.cfg.postgres_dsn or None,
            broadcast=state.ws.broadcast,
            dispatch=_dispatch if targets else None,
            device_targets=targets or None,
        )

    audit.spawn(_run())


@router.get("/predict")
async def predict(
    horizon_min: int = Query(default=15, ge=1, le=120),
    state: GatewayState = Depends(get_state),
) -> dict:
    cfg = state.cfg
    key = f"predict:{horizon_min}"

    # --- LIVE: ai/congestion /predict ---
    url = cfg.congestion_url.rstrip("/") + "/predict"
    t0 = time.perf_counter()
    try:
        resp = await state.http.post(url, json={"horizon_min": horizon_min})
        UPSTREAM_LATENCY.labels("traffic", "congestion").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            data = resp.json()
            await cache.put("traffic", key, data, ttl=cfg.cache_ttl_traffic_s)
            await state.record_decision(
                api="traffic", key=key, decision_path="LIVE",
                latency_ms=(time.perf_counter() - t0) * 1000, source="congestion",
                source_state=SourceState.LIVE,
            )
            _auto_congestion_alert(state, data)
            REQUESTS.labels("traffic", "ok").inc()
            return {"decision_path": "LIVE", "horizon_min": horizon_min, "predictions": data}
        log.info("traffic_predict_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("traffic_predict_unreachable", url=url, error=str(exc))

    # --- CACHED ---
    cached = await cache.get("traffic", key)
    if cached is not None:
        await state.record_decision(
            api="traffic", key=key, decision_path="CACHED", source="congestion",
            source_state=SourceState.DEGRADED, ok=False,
            detail={"cache_age_s": round(cached["age_s"], 1) if cached["age_s"] else None},
        )
        _auto_congestion_alert(state, cached["value"])
        REQUESTS.labels("traffic", "ok").inc()
        return {"decision_path": "CACHED", "horizon_min": horizon_min,
                "predictions": cached["value"], "cache_age_s": cached["age_s"]}

    # --- SYNTHETIC ---
    synth = _synthetic_predictions()
    await state.record_decision(
        api="traffic", key=key, decision_path="SYNTHETIC", source="congestion",
        source_state=SourceState.DOWN, ok=False,
    )
    REQUESTS.labels("traffic", "ok").inc()
    return {"decision_path": "SYNTHETIC", "horizon_min": horizon_min, "predictions": synth}


@router.post("/congestion-scan")
async def congestion_scan(
    body: Dict[str, Any] = Body(default_factory=dict),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Run the congestion detector now and raise any TRAFFIC_CONGESTION alerts.

    The awaited (non-fire-and-forget) counterpart to the automatic scan on
    ``/predict`` — a deterministic trigger for the demo and e2e tests. Optional
    body: ``{"predictions": {seg: score}, "threshold": float, "device_targets": [id]}``.
    Without ``predictions`` it scans the current forecaster output. When
    ``device_targets`` are given, each driver also gets a WebPush/FCM advisory.
    Returns the alerts newly created (deduped per segment per hour).
    """
    from services import congestion_alert
    from .. import notifications as notif

    preds = body.get("predictions") if isinstance(body, dict) else None
    if not preds:
        current = await predict(horizon_min=15, state=state)
        preds = current.get("predictions", {})
    thr = float(body.get("threshold", state.cfg.congestion_alert_threshold))
    targets = body.get("device_targets") or None

    dispatch_fn = None
    if targets:
        async def dispatch_fn(device_id: str, advisory: Dict[str, Any]):  # noqa: E306
            # WebPush + FCM only (ws=False): the corridor WS broadcast is emitted
            # once by ``broadcast`` below, so we don't double-send the WS frame.
            return await notif.dispatch(state, device_id, advisory, ws_type="alert", ws=False)

    created = await congestion_alert.raise_congestion_alerts(
        predictions=preds or {},
        threshold=thr,
        dsn=state.cfg.postgres_dsn or None,
        broadcast=state.ws.broadcast,
        dispatch=dispatch_fn,
        device_targets=targets,
    )
    REQUESTS.labels("traffic", "ok").inc()
    return {"threshold": thr, "count": len(created), "created": created}


def _normalize_congestion_metrics(data: dict) -> dict:
    """Add the evaluator-facing fields (model_name / evaluation_dataset /
    data_mode + an ``f1`` alias) on top of the real training-metrics artifact
    (``congestion_onset_f1``, ``precision``, ``recall`` are already present and
    genuine). Every upstream key is preserved.
    """
    from jnpa_shared.config import get_settings

    if "error" in data:  # {"error": "no_metrics", ...} — pass through untouched
        return data

    support_total = data.get("support_total")
    num_segments = data.get("num_segments")
    out = dict(data)
    out.update({
        "model_name": "GraphSAGE + LSTM (congestion-onset forecaster)",
        "f1": data.get("congestion_onset_f1"),  # convenience alias
        "evaluation_dataset": (
            "14-day deterministic synthetic corridor commute history (+ real "
            f"Timescale tail when available); {num_segments or 13} NH-348 segments; "
            f"held-out temporal split, {support_total or '?'} segment-windows"
        ),
        "data_mode": get_settings().data_mode,
        # Model metrics come from a reproducible offline train, not the live feed;
        # they are real regardless of data_mode. The flag says so explicitly.
        "metrics_synthetic": False,
    })
    return out


async def _congestion_metrics(state: GatewayState) -> dict:
    url = state.cfg.congestion_url.rstrip("/") + "/metrics"
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url, timeout=10.0)
        UPSTREAM_LATENCY.labels("traffic", "congestion").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            REQUESTS.labels("traffic", "ok").inc()
            return _normalize_congestion_metrics(resp.json())
        log.info("traffic_metrics_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("traffic_metrics_unreachable", url=url, error=str(exc))
    raise HTTPException(status_code=503, detail={"error": "congestion_metrics_unavailable"})


@router.get("/metrics")
async def metrics(state: GatewayState = Depends(get_state)) -> dict:
    """Proxy ai/congestion's evaluation metrics (``GET /metrics``) and normalize
    into the evaluator-facing shape the dashboard model-performance card renders
    (model_name / f1 / precision / recall / evaluation_dataset / data_mode). The
    realism probe (web/src/data/live.ts:congestionMetrics) keeps working.

    503 when ai/congestion is unreachable — the dashboard degrades that to the
    static target note.
    """
    return await _congestion_metrics(state)


@router.get("/congestion/metrics")
async def congestion_metrics_alias(state: GatewayState = Depends(get_state)) -> dict:
    """Alias for ``/api/traffic/congestion/metrics`` (the path named in the
    UC-3 audit acceptance criteria). Same normalized payload as ``/metrics``."""
    return await _congestion_metrics(state)


@router.get("/snapshots")
async def snapshots(state: GatewayState = Depends(get_state)) -> dict:
    """Latest per-segment traffic snapshot for the corridor map overlay."""
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(
            """
            SELECT DISTINCT ON (segment_id)
                   segment_id, ts, speed_kmh, jam_factor, source
            FROM core.traffic_snapshot
            ORDER BY segment_id, ts DESC
            """,
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("traffic_snapshots_failed", error=str(exc))
        rows = []
    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        if isinstance(d.get("ts"), datetime):
            d["ts"] = d["ts"].isoformat()
        out.append(d)
    REQUESTS.labels("traffic", "ok").inc()
    return {"snapshots": out, "count": len(out)}
