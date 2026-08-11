"""/api/vahan — orchestrated Vahan / Sarathi / FastTag with the 4-rung chain.

    LIVE_PRIMARY  -> ULIP         (only if ULIP_LIVE_ENABLED=1)
                     VAHAN/04 -> VAHAN/01 for RC, SARATHI/02 for DL
    LIVE_FALLBACK -> vahan-sim
    CACHED        -> last good response from Redis (TTL 12 h)
    PROVISIONAL   -> admit vehicle with provisional=true + 24 h cure window,
                     write core.vehicle_rc(provisional_until=now()+24h),
                     emit Alert(kind=PROVISIONAL_VEHICLE).

The chosen rung is recorded via ``state.record_decision(..., decision_path=...)``
so the demo can show which path served each request (``/api/debug/decisions``).
"""
from __future__ import annotations

import re
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

# SARATHI/01's documented date-of-birth pattern, checked here so a bad value
# never silently downgrades the request to SARATHI/02.
_RE_DOB = re.compile(r"^\d{4}-(0[1-9]|1[012])-(0[1-9]|[12][0-9]|3[01])$")

router = APIRouter(prefix="/api/vahan", tags=["vahan"])


async def _try_upstream(
    state: GatewayState, base_url: str, path: str, target: str
) -> Optional[dict]:
    """GET base_url+path; return JSON on 200, None on any miss/error.

    A 503 (upstream disabled), connection error, timeout, or non-200 all map
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
    except Exception as exc:  # noqa: BLE001
        # Anything the transport stack raises that is NOT an httpx error still
        # means "this rung did not answer" — e.g. a proxy speaking a broken
        # SOCKS handshake raises socksio.ProtocolError, which sailed past the
        # clause above and turned a routine miss into a 500. A rung failing is
        # the ladder working; it must never be able to fail the request.
        log.warning("vahan_upstream_transport_error", url=url,
                    error=f"{type(exc).__name__}: {exc}")
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


# --------------------------------------------------------------- ULIP (LIVE_PRIMARY)
_ulip_client: Optional["UlipClient"] = None


def _ulip(cfg: Any) -> "UlipClient":
    """The shared ULIP client, built once per process."""
    global _ulip_client
    if _ulip_client is None:
        from integrations.ulip import UlipClient

        _ulip_client = UlipClient(
            api_url=getattr(cfg, "ulip_api_url", "") or None,
            api_key=getattr(cfg, "ulip_api_key", None),
            client_id=getattr(cfg, "ulip_client_id", None),
            client_secret=getattr(cfg, "ulip_client_secret", None),
        )
    return _ulip_client


async def _ulip_rc(state: GatewayState, plate: str) -> Optional[dict]:
    """VAHAN/04 -> a VahanRecord payload, or None on any miss.

    VAHAN/01 is tried when /04 comes back empty: the two are fed by different
    upstream calls, so one can answer where the other misses. Both are mapped
    to the same record shape, so the rung below cannot tell which replied.

    Never raises: every ULIP failure is a miss that drops to the next rung,
    exactly like an unreachable upstream. A vehicle must not be refused at the
    gate because a national gateway had a bad minute.
    """
    from integrations.ulip import UlipError
    from integrations.ulip.records import rc_payload, rc_to_record
    from integrations.ulip.schemas import normalize_rc, normalize_vahan_xml

    client = _ulip(state.cfg)
    t0 = time.perf_counter()
    try:
        envelope = await client.fetch_vehicle_by_rc(plate)
        fields = _matching_rc(normalize_rc(envelope), plate, "VAHAN/04")
        if not fields:
            envelope = await client.fetch_vehicle_by_rc_xml(plate)
            fields = _matching_rc(normalize_vahan_xml(envelope), plate, "VAHAN/01")
    except UlipError as exc:
        log.warning("vahan_ulip_miss", plate=plate, error=type(exc).__name__)
        return None
    finally:
        UPSTREAM_LATENCY.labels("vahan", "ulip").observe(time.perf_counter() - t0)
    record = rc_to_record(fields or {})
    return rc_payload(record) if record else None


def _matching_rc(fields: Optional[dict], plate: str,
                 api: str) -> Optional[dict]:
    """Drop an RC that is not for the plate we asked about.

    VAHAN/01 on staging answers a *different* registration for the same input
    on roughly half of all calls — asking for ``UP32KH0320`` returns
    ``RJ11GC0346`` (a different make, class and owner) about as often as it
    returns the right vehicle. VAHAN/04 is stable. Whatever the cause, binding
    a stranger's registration to a plate at the gate is the worst failure this
    module can produce: it would clear a truck on someone else's fitness and
    blacklist status. So the answer is checked against the question, and a
    mismatch is treated as a miss and dropped to the next rung.
    """
    if not fields:
        return None
    returned = normalize_plate(str(fields.get("rc_number") or ""))
    if returned and returned != normalize_plate(plate):
        log.warning("vahan_ulip_plate_mismatch", api=api,
                    requested=plate, returned=returned)
        return None
    return fields


async def _ulip_dl(state: GatewayState, dl: str,
                   dob: Optional[str] = None) -> Optional[dict]:
    """SARATHI/02 -> a SarathiRecord payload, or None on any miss.

    When the caller supplies the holder's date of birth, **SARATHI/01** is
    tried first and SARATHI/02 is the fallback. /01 needs the extra identifier
    but answers with strictly more: the licence issue date, the issuing state
    and RTO, and — uniquely among the granted APIs — the holder's name
    unmasked plus a photograph. That is the difference between a licence check
    and enough identity to issue a port pass, so it is worth the extra field
    whenever enrolment has it. The gate itself never does, which is why /02
    remains the default path.
    """
    from integrations.ulip import UlipError
    from integrations.ulip.records import dl_to_record
    from integrations.ulip.schemas import normalize_dl

    client = _ulip(state.cfg)
    t0 = time.perf_counter()
    try:
        # SARATHI/01 is spacing-sensitive and SARATHI/02 is not, so both
        # spellings are attempted before concluding the licence is unknown.
        spellings = [dl] if " " not in dl else [dl, dl.replace(" ", "")]
        fields = None
        if dob:
            for spelling in spellings:
                try:
                    fields = normalize_dl(
                        await client.fetch_dl_with_dob(spelling, dob))
                except UlipError as exc:
                    # A bad DOB, or a licence /01 does not hold, must not cost
                    # the caller the ordinary /02 answer.
                    log.info("sarathi01_miss", error=type(exc).__name__)
                    continue
                if fields:
                    break
        if not fields:
            for spelling in spellings:
                fields = normalize_dl(await client.fetch_dl(spelling))
                if fields:
                    break
    except UlipError as exc:
        log.warning("sarathi_ulip_miss", dl=dl, error=type(exc).__name__)
        return None
    finally:
        UPSTREAM_LATENCY.labels("vahan", "ulip").observe(time.perf_counter() - t0)
    record = dl_to_record(dl, fields or {})
    return record.model_dump(mode="json") if record else None


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

    # --- Rung 1: LIVE_PRIMARY (ULIP VAHAN/04 -> /01) — only when enabled ---
    # This is UC3's first real RC source: the previous Surepass rung was never
    # exercised because SUREPASS_API_TOKEN was empty in every environment.
    if cfg.ulip_live_enabled and not skip_live and not skip_primary:
        t0 = time.perf_counter()
        data = await _ulip_rc(state, plate)
        if data is not None:
            await cache.put("vahan", plate, data, ttl=cfg.cache_ttl_vahan_s)
            await state.record_decision(
                api="vahan", key=plate, decision_path=VahanPath.LIVE_PRIMARY.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="ulip",
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
            source_state=SourceState.DEGRADED if cfg.ulip_live_enabled else SourceState.LIVE,
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
async def sarathi_dl(
    dl_number: str,
    dob: Optional[str] = Query(
        None, description="Holder's date of birth as YYYY-MM-DD. When given, "
                          "SARATHI/01 is tried first (richer record, unmasked "
                          "name, photograph); SARATHI/02 remains the fallback."),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Sarathi DL lookup — LIVE_PRIMARY -> LIVE_FALLBACK -> CACHED.

    DLs have no provisional rung (a licence cannot be "admitted on trust"); a
    full miss returns 404.
    """
    cfg = state.cfg
    # Two forms of the same licence, and the difference is load-bearing.
    # SARATHI/01 matches on the RTO's own spacing: "GJ04 20120005008" resolves
    # and "GJ0420120005008" answers errorcd -1. Stripping the space — which is
    # what we used to do before calling upstream — made /01 unable to resolve
    # ANY licence written in the standard format. The space-free form stays the
    # canonical key for the cache and the history tables so one licence cannot
    # occupy two rows.
    dl_as_given = " ".join(dl_number.strip().upper().split())
    dl = dl_as_given.replace(" ", "")
    if dob and not _RE_DOB.match(dob.strip()):
        # Falling back to SARATHI/02 on a malformed date would answer 200 with
        # a thinner record than the caller asked for, and they would have no
        # way to tell. A bad argument is the caller's to fix.
        raise HTTPException(status_code=422, detail={
            "error": "invalid_dob",
            "expected": "YYYY-MM-DD (SARATHI/01 field pattern)",
            "received": dob})
    path = f"/sarathi/dl/{dl}"
    # The DOB selects a different upstream API with a richer record, so a
    # cached /02 answer must not be served for a /01 request or vice versa.
    cache_key = f"{dl}|{dob}" if dob else dl

    # LIVE_PRIMARY is ULIP SARATHI/02 (a direct client call, not an HTTP
    # upstream); LIVE_FALLBACK is still the vahan-sim service.
    for kind, target, enabled, fetch in (
        ("LIVE_PRIMARY", "ulip", cfg.ulip_live_enabled,
         lambda: _ulip_dl(state, dl_as_given, dob)),
        ("LIVE_FALLBACK", "vahan-sim", True,
         lambda: _try_upstream(state, cfg.vahan_sim_url, path, "vahan-sim")),
    ):
        if not enabled:
            continue
        t0 = time.perf_counter()
        data = await fetch()
        if data is not None:
            await cache.put("sarathi", cache_key, data, ttl=cfg.cache_ttl_vahan_s)
            await state.record_decision(
                api="vahan", key=dl, decision_path=kind,
                latency_ms=(time.perf_counter() - t0) * 1000, source=target,
            )
            _persist_dl(state, dl, data, kind)
            REQUESTS.labels("vahan", "ok").inc()
            return {"dl": dl, "decision_path": kind, "record": data,
                    "status": vehicle_intel.dl_status(data)}

    cached = await cache.get("sarathi", cache_key)
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


@router.get("/chassis/{chassis_number}",
            summary="RC particulars by chassis number (ULIP VAHAN/02)")
async def vahan_by_chassis(chassis_number: str,
                           state: GatewayState = Depends(get_state)) -> dict:
    """Look up a vehicle by chassis number.

    No simulator rung: vahan-sim is keyed by plate only, so this is ULIP or
    nothing. A miss is a 404 rather than a fallback — silently answering from
    a different vehicle would be far worse than no answer.
    """
    return await _by_alternate_key(state, "chassis", chassis_number)


@router.get("/engine/{engine_number}",
            summary="RC particulars by engine number (ULIP VAHAN/03)")
async def vahan_by_engine(engine_number: str,
                          state: GatewayState = Depends(get_state)) -> dict:
    """Look up a vehicle by engine number. Same posture as ``/chassis``."""
    return await _by_alternate_key(state, "engine", engine_number)


async def _by_alternate_key(state: GatewayState, kind: str, value: str) -> dict:
    """Shared body for the chassis/engine lookups (ULIP VAHAN/02 and /03)."""
    from integrations.ulip import UlipError, UlipInvalidRequest
    from integrations.ulip.records import rc_payload, rc_to_record
    from integrations.ulip.schemas import normalize_vahan_xml

    cfg = state.cfg
    key = value.strip().upper()
    if not cfg.ulip_live_enabled:
        REQUESTS.labels("vahan", "not_found").inc()
        raise HTTPException(status_code=503, detail={
            "error": "ulip_disabled",
            "detail": f"{kind} lookup is served only by ULIP; set ULIP_LIVE_ENABLED=1",
        })
    client = _ulip(cfg)
    fetch = (client.fetch_vehicle_by_chassis if kind == "chassis"
             else client.fetch_vehicle_by_engine)
    t0 = time.perf_counter()
    try:
        envelope = await fetch(key)
    except UlipInvalidRequest as exc:
        REQUESTS.labels("vahan", "invalid").inc()
        raise HTTPException(status_code=422,
                            detail={"error": f"invalid_{kind}", "detail": str(exc)})
    except UlipError as exc:
        REQUESTS.labels("vahan", "error").inc()
        log.warning("vahan_alt_key_upstream_failed", kind=kind,
                    error=type(exc).__name__)
        raise HTTPException(status_code=502,
                            detail={"error": "upstream_error",
                                    "detail": type(exc).__name__})
    finally:
        UPSTREAM_LATENCY.labels("vahan", "ulip").observe(time.perf_counter() - t0)
    # VAHAN/02 and /03 answer XML-in-JSON, never the native-JSON shape.
    record = rc_to_record(normalize_vahan_xml(envelope) or {})
    if record is None:
        REQUESTS.labels("vahan", "not_found").inc()
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", kind: key})
    data = rc_payload(record)
    await state.record_decision(
        api="vahan", key=key, decision_path=VahanPath.LIVE_PRIMARY.value,
        latency_ms=(time.perf_counter() - t0) * 1000, source="ulip",
        source_state=SourceState.LIVE,
    )
    audit.spawn(vehicle_intel.record_vehicle_verification(
        vehicle_number=data.get("rc_number"), request={kind: key},
        response=data, status="FOUND", source="ULIP", dsn=cfg.postgres_dsn))
    REQUESTS.labels("vahan", "ok").inc()
    return {kind: key, "decision_path": VahanPath.LIVE_PRIMARY.value,
            "record": data}


@router.get("/fastag/{plate}")
async def fastag_balance(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """FastTag balance — LIVE_FALLBACK -> CACHED (no provisional).

    There is NO live rung here and there cannot be one: ULIP grants no
    wallet-balance API (FASTAG/01 is toll crossings, FASTAG/02 is the tag
    registry), and the Surepass path that used to sit here was never
    configured. The simulator is therefore the only source, which is exactly
    what ``decision_path: LIVE_FALLBACK`` reports — see /api/fastag/balance for
    the durable-snapshot surface.
    """
    cfg = state.cfg
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        REQUESTS.labels("vahan", "invalid").inc()
        raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
    path = f"/fastag/balance/{norm}"

    for kind, base_url, target, primary in (
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


@router.get("/vehicle-360/{plate}")
async def vehicle_360(plate: str, state: GatewayState = Depends(get_state)) -> dict:
    """Vehicle 360: master row + assigned driver + licence/PDP + transport company
    + compliance + alerts + lifecycle timeline, in one response.

    An aggregate over tables that already exist — it reuses vehicle_intel() for
    the enforcement/telemetry half and adds only the master-data spine, so
    /vehicle-intel keeps its exact contract for existing callers.
    """
    norm = normalize_plate(plate)
    data = await vehicle_intel.vehicle_360(norm, dsn=state.cfg.postgres_dsn)
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
