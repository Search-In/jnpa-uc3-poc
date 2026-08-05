"""/api/traffic — orchestrated corridor traffic with a LIVE/CACHED/SYNTHETIC chain.

    LIVE       -> ai/congestion /predict (per-segment P(congested))
    CACHED      -> last good /predict result from Redis (TTL 90 s)
    SYNTHETIC  -> deterministic free-flow-ish synthetic probabilities so the
                  dashboard's corridor heat-map never goes blank

Also exposes ``/api/traffic/snapshots`` reading the latest per-segment
``core.traffic_snapshot`` rows for the live map overlay.

ADDITIVE — TomTom live traffic intelligence (same mould as
gateway/routers/weather.py: a thin router over
:class:`services.traffic.TrafficService` → integrations/tomtom, key-gated via
TOMTOM_API_KEY, backend-only, never exposed to the browser; a provider outage
NEVER breaks this surface — LIVE → CACHED (Redis) → DATABASE → SYNTHETIC):

    GET /api/traffic/current  -> normalised flow + incidents for the corridor
    GET /api/traffic/health   -> TomTom integration posture

The pre-existing congestion-model endpoints (/predict, /congestion-scan,
/metrics, /snapshots) are untouched.
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from jnpa_shared import corridor
from jnpa_shared.config import get_settings

from .. import cache
from ..fallback import SourceState
from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

from services.traffic import TrafficService

log = get_logger("gateway.traffic")

router = APIRouter(prefix="/api/traffic", tags=["traffic"])

_service: Optional[TrafficService] = None


def get_service(request: Request) -> TrafficService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        from integrations.tomtom import TomTomClient

        _service = TrafficService(
            dsn=getattr(cfg, "postgres_dsn", None) or None,
            # Key empty -> LIVE rung disabled; the surface still answers from
            # the CACHED/DATABASE/SYNTHETIC rungs and says so in the metadata.
            client=TomTomClient(
                api_key=getattr(cfg, "tomtom_api_key", None),
                flow_url=getattr(cfg, "tomtom_flow_url", "") or None,
                incidents_url=getattr(cfg, "tomtom_incidents_url", "") or None,
                routing_url=getattr(cfg, "tomtom_routing_url", "") or None,
            ),
            cache_ttl_s=getattr(cfg, "cache_ttl_tomtom_s", None) or 120,
        )
    return _service


def _default_coords(latitude: Optional[float], longitude: Optional[float]) -> tuple[float, float]:
    """Fall back to the configured JNPA port coordinates (env-driven, not hardcoded)."""
    s = get_settings()
    return (latitude if latitude is not None else s.port_lat,
            longitude if longitude is not None else s.port_lon)


# ------------------------------------------------------------------- current
@router.get("/current",
            summary="TomTom traffic flow + incidents for the JNPA corridor")
async def current_traffic(
    latitude: Optional[float] = Query(None, ge=-90, le=90),
    longitude: Optional[float] = Query(None, ge=-180, le=180),
    svc: TrafficService = Depends(get_service),
) -> Dict[str, Any]:
    lat, lon = _default_coords(latitude, longitude)
    result = await svc.current(lat, lon)
    REQUESTS.labels("traffic", "ok" if result["status"] == "LIVE" else "error").inc()
    return result


# ------------------------------------------------------------------- health
@router.get("/health", summary="TomTom traffic integration posture")
async def traffic_health(svc: TrafficService = Depends(get_service)) -> Dict[str, Any]:
    s = get_settings()
    client = svc._client  # noqa: SLF001 - posture only; the key itself is never returned
    return {
        "system": "TRAFFIC",
        "provider": "TOMTOM",
        "configured": client.configured,
        "api_key_required": True,
        "flow_url": client.flow_url,
        "incidents_url": client.incidents_url,
        "timeout_s": client.timeout_s,
        "retries": client.retries,
        "cache_ttl_s": svc.cache_ttl_s,
        "default_location": {"latitude": s.port_lat, "longitude": s.port_lon},
    }


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
    from .. import mailer
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
            # Admin email for a NEW alert only; a no-op when ADMIN_ALERT_EMAILS is unset.
            email_notify=mailer.notify_congestion_alert,
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
    from .. import mailer
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
        # Admin email for a NEW alert only; a no-op when ADMIN_ALERT_EMAILS is unset.
        email_notify=mailer.notify_congestion_alert,
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


# The congestion service's ``GET /metrics`` does exactly one thing: read the
# persisted training summary from ``artifacts/metrics.json`` (ai/congestion/infer.py
# :metrics_summary). That artifact is committed to the repo, so the gateway can
# read the SAME FILE when the service itself is not running — identical numbers
# from an identical source, not a synthesised stand-in. Overridable for images
# that mount it elsewhere.
_CONGESTION_METRICS_PATH_ENV = "CONGESTION_METRICS_PATH"
_CONGESTION_METRICS_DEFAULT = "ai/congestion/artifacts/metrics.json"


def _congestion_metrics_artifact() -> Optional[dict]:
    """LOCAL-ARTIFACT rung: the committed training-metrics summary, or None.

    Deliberately NOT a synthetic generator — if the artifact is absent this
    returns None and the caller still raises 503. Model-performance numbers are
    evidential; they are never invented here.
    """
    import json
    import os
    from pathlib import Path

    candidates = []
    override = os.environ.get(_CONGESTION_METRICS_PATH_ENV, "").strip()
    if override:
        candidates.append(Path(override))
    # Repo root is three parents up from gateway/routers/traffic.py; the container
    # image copies the artifact to the same relative location (gateway/Dockerfile).
    repo_root = Path(__file__).resolve().parents[2]
    candidates.append(repo_root / _CONGESTION_METRICS_DEFAULT)
    candidates.append(Path("/app") / _CONGESTION_METRICS_DEFAULT)

    for path in candidates:
        try:
            if path.is_file():
                data = json.loads(path.read_text())
                if isinstance(data, dict) and data:
                    log.info("traffic_metrics_artifact_hit", path=str(path))
                    return data
        except Exception as exc:  # noqa: BLE001 — a bad artifact must not 500
            log.warning("traffic_metrics_artifact_unreadable", path=str(path),
                        error=str(exc))
    return None


async def _congestion_metrics(state: GatewayState) -> dict:
    """Model-performance metrics: LIVE (ai/congestion) -> LOCAL-ARTIFACT -> 503.

    The service was the only source, so a stopped ai/congestion container 503'd
    this endpoint and blanked the dashboard's model-performance card. Since the
    service merely serves a committed artifact, the gateway now reads that same
    artifact as a second rung and labels the provenance
    (``decision_path: LOCAL_ARTIFACT``) so a cached read can never be mistaken for
    a live one. If the artifact is missing too, the 503 stands — no fabricated
    numbers.
    """
    url = state.cfg.congestion_url.rstrip("/") + "/metrics"
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url, timeout=10.0)
        UPSTREAM_LATENCY.labels("traffic", "congestion").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            REQUESTS.labels("traffic", "ok").inc()
            out = _normalize_congestion_metrics(resp.json())
            if isinstance(out, dict) and "error" not in out:
                out.setdefault("decision_path", "LIVE")
                out.setdefault("source", "ai/congestion")
            return out
        log.info("traffic_metrics_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("traffic_metrics_unreachable", url=url, error=str(exc))

    artifact = _congestion_metrics_artifact()
    if artifact is not None:
        REQUESTS.labels("traffic", "degraded").inc()
        out = _normalize_congestion_metrics(artifact)
        if isinstance(out, dict) and "error" not in out:
            out["decision_path"] = "LOCAL_ARTIFACT"
            out["source"] = "ai/congestion artifacts/metrics.json"
            out["live_service_available"] = False
        return out

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
