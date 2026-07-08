"""/api/vahan — orchestrated Vahan / Sarathi / FastTag with the 4-rung chain.

    LIVE_PRIMARY  -> vahan-live   (only if SUREPASS_API_TOKEN is set)
    LIVE_FALLBACK -> vahan-sim
    CACHED        -> last good response from Redis (TTL 12 h)
    PROVISIONAL   -> admit vehicle with provisional=true + 24 h cure window,
                     write jnpa.vehicle_master(provisional_until=now()+24h),
                     emit Alert(kind=PROVISIONAL_VEHICLE).

The chosen rung is recorded via ``state.record_decision(..., decision_path=...)``
so the demo can show which path served each request (``/api/debug/decisions``).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from jnpa_shared.schemas import is_valid_plate, normalize_plate

from .. import audit, cache
from .. import vehicle_intel
from ..fallback import SourceState, VahanPath
from ..logging import get_logger
from ..metrics import PROVISIONAL, REQUESTS, UPSTREAM_LATENCY
from ..provisional import (
    admit_provisional,
    build_provisional_alert,
    persist_alert,
)
from ..state import GatewayState, get_state

log = get_logger("gateway.vahan")

router = APIRouter(prefix="/api/vahan", tags=["vahan"])


async def _try_upstream(
    state: GatewayState, base_url: str, path: str, target: str
) -> Optional[dict]:
    """GET base_url+path; return JSON on 200, None on any miss/error.

    A 503 (vahan-live disabled), connection error, timeout, or non-200 all map
    to None so the orchestrator simply drops to the next rung. A 422 (invalid
    input) is surfaced as an exception by the caller path instead.
    """
    url = base_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url)
    except httpx.HTTPError as exc:
        log.warning("vahan_upstream_unreachable", url=url, error=str(exc))
        return None
    finally:
        UPSTREAM_LATENCY.labels("vahan", target).observe(time.perf_counter() - t0)
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            return None
    if resp.status_code == 422:
        # Bad plate/DL — propagate as a client error rather than falling back.
        raise HTTPException(status_code=422, detail=_safe_detail(resp))
    log.info("vahan_upstream_miss", url=url, status=resp.status_code)
    return None


def _safe_detail(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
        return body.get("detail", body) if isinstance(body, dict) else body
    except ValueError:
        return {"error": "upstream_error", "status": resp.status_code}


async def _orchestrate_rc(state: GatewayState, plate: str) -> dict:
    """Run the 4-rung Vahan RC chain for a normalised, validated plate."""
    cfg = state.cfg
    path = f"/vahan/rc/{plate}"

    # Presenter fault injection: a forced rung short-circuits the cascade.
    #   PROVISIONAL  -> jump straight to the 24-hr cure path (the headline demo)
    #   CACHED       -> skip the live upstreams, try cache then provisional
    #   LIVE_FALLBACK-> skip the primary, serve from vahan-sim
    forced = state.faults.forced("vahan")
    if forced == VahanPath.PROVISIONAL.value:
        return await _provisional(state, plate)
    skip_live = forced in (VahanPath.CACHED.value, VahanPath.PROVISIONAL.value)
    skip_primary = forced == VahanPath.LIVE_FALLBACK.value

    # --- Rung 1: LIVE_PRIMARY (vahan-live) — only when a token is configured ---
    if cfg.surepass_enabled and not skip_live and not skip_primary:
        t0 = time.perf_counter()
        data = await _try_upstream(state, cfg.vahan_live_url, path, "vahan-live")
        if data is not None:
            await cache.put("vahan", plate, data, ttl=cfg.cache_ttl_vahan_s)
            await state.record_decision(
                api="vahan", key=plate, decision_path=VahanPath.LIVE_PRIMARY.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="vahan-live",
                source_state=SourceState.LIVE,
            )
            return _envelope(data, VahanPath.LIVE_PRIMARY.value, plate)

    # --- Rung 2: LIVE_FALLBACK (vahan-sim) ---
    t0 = time.perf_counter()
    data = None if skip_live else await _try_upstream(state, cfg.vahan_sim_url, path, "vahan-sim")
    if data is not None:
        await cache.put("vahan", plate, data, ttl=cfg.cache_ttl_vahan_s)
        await state.record_decision(
            api="vahan", key=plate, decision_path=VahanPath.LIVE_FALLBACK.value,
            latency_ms=(time.perf_counter() - t0) * 1000, source="vahan-sim",
            source_state=SourceState.DEGRADED if cfg.surepass_enabled else SourceState.LIVE,
        )
        return _envelope(data, VahanPath.LIVE_FALLBACK.value, plate)

    # --- Rung 3: CACHED (last good response, TTL 12 h) ---
    cached = await cache.get("vahan", plate)
    if cached is not None:
        await state.record_decision(
            api="vahan", key=plate, decision_path=VahanPath.CACHED.value,
            source="vahan", source_state=SourceState.DEGRADED, ok=False,
            detail={"cache_age_s": round(cached["age_s"], 1) if cached["age_s"] else None},
        )
        return _envelope(cached["value"], VahanPath.CACHED.value, plate,
                         cache_age_s=cached["age_s"])

    # --- Rung 4: PROVISIONAL (admit on trust, 24 h cure window) ---
    return await _provisional(state, plate)


async def _provisional(state: GatewayState, plate: str) -> dict:
    cfg = state.cfg
    reason = "all_vahan_paths_exhausted"
    provisional_until = None
    db_ok = True
    try:
        provisional_until = await admit_provisional(
            plate, dsn=cfg.postgres_dsn, window_h=cfg.provisional_window_h, reason=reason,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        db_ok = False
        log.warning("provisional_writeback_failed", plate=plate, error=str(exc))

    alert = build_provisional_alert(
        plate, provisional_until or _fallback_until(cfg), reason=reason,
    )
    try:
        await persist_alert(alert, dsn=cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover
        log.warning("provisional_alert_persist_failed", plate=plate, error=str(exc))
    # Surface the alert to live dashboards too.
    await state.ws.broadcast("alert", alert.model_dump(mode="json"))

    PROVISIONAL.inc()
    await state.record_decision(
        api="vahan", key=plate, decision_path=VahanPath.PROVISIONAL.value,
        source="vahan", source_state=SourceState.DOWN, ok=False,
        detail={"provisional": True, "db_written": db_ok,
                "provisional_until": (provisional_until or _fallback_until(cfg)).isoformat(),
                "alert_id": str(alert.id)},
    )
    record = {
        "rc_number": plate,
        "plate": plate,
        "provisional": True,
        "provisional_until": (provisional_until or _fallback_until(cfg)).isoformat(),
        "blacklist_status": "CLEAR",
    }
    return _envelope(record, VahanPath.PROVISIONAL.value, plate,
                     provisional=True, alert_id=str(alert.id))


def _fallback_until(cfg):
    from datetime import datetime, timedelta, timezone
    return datetime.now(tz=timezone.utc) + timedelta(hours=cfg.provisional_window_h)


def _envelope(data: dict, decision_path: str, plate: str, **extra: Any) -> dict:
    """Wrap the upstream record with the orchestration metadata the demo shows.

    The vehicle record is returned under ``record`` and the rung under
    ``decision_path`` so the dashboard / curl can read both in one response.
    """
    out = {"plate": plate, "decision_path": decision_path, "record": data}
    out.update(extra)
    return out


@router.get("/rc/{plate}")
async def vahan_rc(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        REQUESTS.labels("vahan", "invalid").inc()
        raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
    result = await _orchestrate_rc(state, norm)
    # Persist verification history + an explicit api_audit_log row (keyed by plate).
    dpath = result.get("decision_path", "")
    status = ("PROVISIONAL" if dpath == "PROVISIONAL"
              else "VERIFIED" if result.get("record") else "NOT_FOUND")
    audit.spawn(vehicle_intel.record_vehicle_verification(
        vehicle_number=norm, request={"vehicle_number": norm}, response=result,
        status=status, source=dpath, dsn=state.cfg.postgres_dsn))
    audit.spawn(audit.log_api_audit(
        service_name="vahan", endpoint=f"GET /api/vahan/rc/{norm}", method="GET",
        request_payload={"vehicle_number": norm}, response_payload=result,
        status_code=200, transaction_id=norm, dsn=state.cfg.postgres_dsn))
    REQUESTS.labels("vahan", "ok").inc()
    return result


def _persist_dl(state: GatewayState, dl: str, record: Optional[dict], source: str) -> None:
    """Persist a DL lookup: history + api_audit_log + drivers upsert (all reused)."""
    status = vehicle_intel.dl_status(record) if record else "NOT_FOUND"
    audit.spawn(vehicle_intel.record_dl_lookup(
        dl_number=dl, request={"dl_number": dl}, response=record or {},
        status=status, source=source, dsn=state.cfg.postgres_dsn))
    audit.spawn(audit.log_api_audit(
        service_name="sarathi", endpoint=f"GET /api/vahan/dl/{dl}", method="GET",
        request_payload={"dl_number": dl}, response_payload=record or {},
        status_code=200 if record else 404, transaction_id=dl, dsn=state.cfg.postgres_dsn))
    if record:
        audit.spawn(vehicle_intel.upsert_driver_from_dl(
            dl_number=dl, record=record, dsn=state.cfg.postgres_dsn))


@router.get("/dl/{dl_number}")
async def sarathi_dl(dl_number: str, state: GatewayState = Depends(get_state)) -> dict:
    """Sarathi DL lookup — LIVE_PRIMARY -> LIVE_FALLBACK -> CACHED.

    DLs have no provisional rung (a licence cannot be "admitted on trust"); a
    full miss returns 404.
    """
    cfg = state.cfg
    dl = dl_number.strip().upper().replace(" ", "")
    path = f"/sarathi/dl/{dl}"

    for kind, base_url, target, primary in (
        ("LIVE_PRIMARY", cfg.vahan_live_url, "vahan-live", cfg.surepass_enabled),
        ("LIVE_FALLBACK", cfg.vahan_sim_url, "vahan-sim", True),
    ):
        if not primary:
            continue
        t0 = time.perf_counter()
        data = await _try_upstream(state, base_url, path, target)
        if data is not None:
            await cache.put("sarathi", dl, data, ttl=cfg.cache_ttl_vahan_s)
            await state.record_decision(
                api="vahan", key=dl, decision_path=kind,
                latency_ms=(time.perf_counter() - t0) * 1000, source=target,
            )
            _persist_dl(state, dl, data, kind)
            REQUESTS.labels("vahan", "ok").inc()
            return {"dl": dl, "decision_path": kind, "record": data,
                    "status": vehicle_intel.dl_status(data)}

    cached = await cache.get("sarathi", dl)
    if cached is not None:
        await state.record_decision(api="vahan", key=dl, decision_path="CACHED",
                                    source="sarathi", source_state=SourceState.DEGRADED, ok=False)
        _persist_dl(state, dl, cached["value"], "CACHED")
        REQUESTS.labels("vahan", "ok").inc()
        return {"dl": dl, "decision_path": "CACHED", "record": cached["value"],
                "status": vehicle_intel.dl_status(cached["value"]), "cache_age_s": cached["age_s"]}

    _persist_dl(state, dl, None, "NOT_FOUND")
    REQUESTS.labels("vahan", "not_found").inc()
    raise HTTPException(status_code=404, detail={"error": "not_found", "dl": dl})


@router.get("/fastag/{plate}")
async def fastag_balance(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """FastTag balance — LIVE_PRIMARY -> LIVE_FALLBACK -> CACHED (no provisional)."""
    cfg = state.cfg
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        REQUESTS.labels("vahan", "invalid").inc()
        raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
    path = f"/fastag/balance/{norm}"

    for kind, base_url, target, primary in (
        ("LIVE_PRIMARY", cfg.vahan_live_url, "vahan-live", cfg.surepass_enabled),
        ("LIVE_FALLBACK", cfg.vahan_sim_url, "vahan-sim", True),
    ):
        if not primary:
            continue
        t0 = time.perf_counter()
        data = await _try_upstream(state, base_url, path, target)
        if data is not None:
            await cache.put("fastag", norm, data, ttl=cfg.cache_ttl_vahan_s)
            await state.record_decision(
                api="vahan", key=norm, decision_path=kind,
                latency_ms=(time.perf_counter() - t0) * 1000, source=target,
            )
            REQUESTS.labels("vahan", "ok").inc()
            return {"plate": norm, "decision_path": kind, "record": data}

    cached = await cache.get("fastag", norm)
    if cached is not None:
        await state.record_decision(api="vahan", key=norm, decision_path="CACHED",
                                    source="fastag", source_state=SourceState.DEGRADED, ok=False)
        REQUESTS.labels("vahan", "ok").inc()
        return {"plate": norm, "decision_path": "CACHED", "record": cached["value"],
                "cache_age_s": cached["age_s"]}

    REQUESTS.labels("vahan", "not_found").inc()
    raise HTTPException(status_code=404, detail={"error": "not_found", "plate": norm})


# ---------------------------------------------------------------------------
# Vehicle & Driver Intelligence (Phase 2 · Track 4) — RDS-backed aggregates.
# ---------------------------------------------------------------------------
@router.get("/vehicle-intel/{plate}")
async def vehicle_intelligence(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """Aggregate vehicle intelligence: RC + tracking + violations + challans + alerts."""
    norm = normalize_plate(plate)
    data = await vehicle_intel.vehicle_intel(norm, dsn=state.cfg.postgres_dsn)
    REQUESTS.labels("vahan", "ok").inc()
    return data


@router.get("/driver-intel/{driver_key}")
async def driver_intelligence(driver_key: str, state: GatewayState = Depends(get_state)) -> dict:
    """Aggregate driver intelligence: profile + DL history + vehicle + violations + activity."""
    data = await vehicle_intel.driver_intel(driver_key.strip(), dsn=state.cfg.postgres_dsn)
    REQUESTS.labels("vahan", "ok").inc()
    return data


@router.get("/verification-history")
async def verification_history(limit: int = Query(default=100, ge=1, le=1000),
                               state: GatewayState = Depends(get_state)) -> dict:
    rows = await vehicle_intel.verification_history(limit=limit, dsn=state.cfg.postgres_dsn)
    REQUESTS.labels("vahan", "ok").inc()
    return {"count": len(rows), "history": rows}


@router.get("/dl-history")
async def dl_history(limit: int = Query(default=100, ge=1, le=1000),
                     state: GatewayState = Depends(get_state)) -> dict:
    rows = await vehicle_intel.dl_history(limit=limit, dsn=state.cfg.postgres_dsn)
    REQUESTS.labels("vahan", "ok").inc()
    return {"count": len(rows), "history": rows}
