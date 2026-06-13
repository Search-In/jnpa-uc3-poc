"""/api/traffic — orchestrated corridor traffic with a LIVE/CACHED/SYNTHETIC chain.

    LIVE       -> ai/congestion /predict (per-segment P(congested))
    CACHED      -> last good /predict result from Redis (TTL 90 s)
    SYNTHETIC  -> deterministic free-flow-ish synthetic probabilities so the
                  dashboard's corridor heat-map never goes blank

Also exposes ``/api/traffic/snapshots`` reading the latest per-segment
``jnpa.traffic_snapshots`` rows for the live map overlay.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, Query

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


@router.get("/snapshots")
async def snapshots(state: GatewayState = Depends(get_state)) -> dict:
    """Latest per-segment traffic snapshot for the corridor map overlay."""
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(
            """
            SELECT DISTINCT ON (segment_id)
                   segment_id, ts, speed_kmh, jam_factor, source
            FROM jnpa.traffic_snapshots
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
