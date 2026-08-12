"""/api/trucks — trucking-app position with the PRIMARY/SECONDARY/TERTIARY chain.

    PRIMARY   -> trucking-app GPS (the live device, fed by MQTT trucks/+/telemetry)
    SECONDARY -> ULIP relay GPS via /api/ulip/proxy (mock if no ULIP key)
    TERTIARY  -> the latest web check-in submitted at /checkin

For SECONDARY and TERTIARY the vehicle is still allowed through the gate but
under *elevated scrutiny*: an ``Alert(kind=ELEVATED_SCRUTINY)`` is raised and the
gate-boom delay is bumped by +5 s (surfaced in the response as
``gate_boom_delay_s`` so the dashboard / gate controller can honour it).

The gateway keeps the most recent /checkin submissions in a small in-memory map
(per device) so TERTIARY has something to serve during a demo without a DB.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..fallback import SourceState, TruckPath
from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..provisional import build_elevated_scrutiny_alert, persist_alert
from ..state import GatewayState, get_state
from . import push

log = get_logger("gateway.trucks")

router = APIRouter(prefix="/api/trucks", tags=["trucks"])

# The gateway's global upstream timeout (``cfg.upstream_timeout_s``, ~2 s) is
# calibrated for the ANPR *liveness* budget — a lookup slower than 2 s is treated
# as "not LIVE". That budget is wrong for the trucking-app control plane: listing
# / re-routing aggregates over a large in-memory fleet and legitimately takes
# longer than an ANPR frame lookup. Using the 2 s budget here made a healthy
# truck-sim look "unreachable" (``degraded:[]`` on the advisory list, 502
# ``truck_sim_unreachable`` on Push Re-route). Give the truck-sim calls their own,
# generous budget so a busy-but-alive sim is never mistaken for a dead one.
TRUCK_UPSTREAM_TIMEOUT_S = 12.0

# Fleet-LIST probe budget. The Driver-Advisory queue blocks on this call, and a
# healthy truck-sim answers in single-digit milliseconds on the compose network —
# so when the sim is down the OLD 12 s timeout was pure dead air: the Advisory
# table spun for 12 s and then rendered empty. Connect gets 1.5 s (an unreachable
# host fails the SYN, not the read) and the whole exchange 4 s, so the fallback
# ladder runs while the page is still interactive. The 12 s budget remains on the
# single-device probe and the reroute POST, whose callers are not page-blocking.
TRUCK_LIST_TIMEOUT = httpx.Timeout(4.0, connect=1.5)

# In-process memo of the last GOOD /devices/list payload per (state, limit) —
# the CACHED rung of the fleet-list ladder (LIVE → CACHED → RDS tail → check-ins).
#
#   * FRESH (≤ LIST_CACHE_FRESH_S): served without touching the sim at all. Every
#     dashboard surface (Advisory, Command Center, maps) polls this endpoint; one
#     probe per few seconds is enough for data the sim recomputes per tick.
#   * STALE (≤ LIST_CACHE_STALE_S): served ONLY when the sim probe fails, marked
#     degraded + CACHED with its age, so a sim blip shows the last real queue
#     instead of an empty table. This is the rung the state-filtered query never
#     had — it used to degrade straight to empty.
#
# Process-local by design: it must keep working when Redis is down too, and the
# gateway runs single-process. Values are the exact response bodies served.
LIST_CACHE_FRESH_S = 3.0
LIST_CACHE_STALE_S = 600.0
_LIST_CACHE: Dict[str, tuple[float, dict]] = {}


def _trace_list(rid: str, rung: str, state: Optional[str], limit: int,
                body: dict, t0: float, *, status: int = 200) -> dict:
    """ONE structured line per fleet-list answer — the trace the intermittent
    Driver-Advisory queue needs to be diagnosable in a running deployment.

    A miss on this endpoint is invisible in aggregate metrics (it is a 200 with
    an empty list), so the request id, the rung that answered, the count and the
    latency are logged together. Grep `trucks_list_answer` to reconstruct a
    session: every alternation between count=N and degraded=true shows up with
    the probe latency that caused it.
    """
    log.info("trucks_list_answer", request_id=rid, endpoint="/api/trucks",
             state=state, limit=limit, status=status,
             count=body.get("count", len(body.get("devices") or [])),
             degraded=bool(body.get("degraded")),
             decision_path=body.get("decision_path"), source=body.get("source"),
             rung=rung, state_filter_supported=body.get("state_filter_supported"),
             latency_ms=round((time.perf_counter() - t0) * 1000, 1),
             cache_age_s=body.get("cache_age_s"))
    return body


def _list_cache_get(key: str, max_age_s: float) -> Optional[tuple[float, dict]]:
    """(age_s, body) when a memo no older than ``max_age_s`` exists, else None."""
    hit = _LIST_CACHE.get(key)
    if not hit:
        return None
    age = time.monotonic() - hit[0]
    if age > max_age_s:
        return None
    return age, hit[1]

# Most-recent /checkin submission per device (TERTIARY source). In-memory ring;
# the dashboard reads it back through /api/trucks/{id}. Demo-scale.
CHECKINS: Dict[str, dict] = {}

# Most-recent re-route advisory dispatched per device — the PWA's polling
# fallback reads this back via GET /api/trucks/{id}/route/latest.
# HOT CACHE ONLY: the durable copy lives in core.reroute_advisory (migration
# 0115) via services.advisory, so an advisory survives a gateway restart.
LAST_REROUTE: Dict[str, dict] = {}

_ADVISORY_REPO: "AdvisoryRepository | None" = None


def _advisory_repo(gw: GatewayState) -> "AdvisoryRepository":
    """Lazily-built singleton repository bound to the gateway's RDS DSN."""
    global _ADVISORY_REPO
    if _ADVISORY_REPO is None:
        from services.advisory import AdvisoryRepository

        _ADVISORY_REPO = AdvisoryRepository(gw.cfg.postgres_dsn)
    return _ADVISORY_REPO


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _primary(state: GatewayState, device_id: str) -> Optional[dict]:
    """PRIMARY: the live device snapshot from the trucking-app control plane.

    Fully instrumented: every step (URL, status, headers, body, JSON decode) and
    every exit is logged at ERROR level with the concrete exception class and a
    traceback — the original ``except httpx.HTTPError: return None`` hid the real
    cause behind a bare ``error=""`` and a misleading 404. On a 200 the raw
    truck-sim record is returned unchanged (no schema/field parsing happens here —
    the gateway just passes the sim payload through as ``record``, so the PWA
    still reads ``record.plate`` etc.).

    **Returns None on every failure so the caller falls through to
    SECONDARY/TERTIARY.** A transport error is exactly the condition the ULIP
    relay rung exists for; re-raising it turned a truck-sim blip into an HTTP 500
    on ``GET /api/trucks/{id}`` and bypassed the documented fallback ladder
    entirely (the "driver advisory API fails after ~30 s" report: a 12 s
    ReadTimeout re-raised, then retried by the client). Diagnosis is preserved —
    nothing is silenced, the log line is identical — but a dead upstream now
    degrades instead of erroring.
    """
    import traceback

    cfg = state.cfg
    url = cfg.truck_api_url.rstrip("/") + f"/devices/{device_id}"
    log.info("trucks_primary_begin", device_id=device_id, url=url)
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url, timeout=TRUCK_UPSTREAM_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — log EVERYTHING, then degrade
        # repr(exc) exposes the concrete class (e.g. ConnectError('') / ReadError())
        # that str(exc)="" was hiding; the traceback pins the exact failing frame.
        log.error(
            "trucks_primary_exception",
            device_id=device_id, url=url,
            exc_type=type(exc).__name__, exc_repr=repr(exc),
            traceback=traceback.format_exc(),
        )
        return None
    UPSTREAM_LATENCY.labels("trucks", "truck-sim").observe(time.perf_counter() - t0)

    # Log the raw HTTP result BEFORE any interpretation.
    body_preview = resp.text[:500]
    log.info(
        "trucks_primary_response",
        device_id=device_id, status=resp.status_code,
        headers=dict(resp.headers), body_preview=body_preview,
    )

    if resp.status_code == 200:
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — a 200 that isn't valid JSON
            log.error(
                "trucks_primary_json_error",
                device_id=device_id, exc_repr=repr(exc),
                body_preview=body_preview, traceback=traceback.format_exc(),
            )
            return None  # malformed upstream body -> degrade, don't 500
        log.info("trucks_primary_return_ok", device_id=device_id, keys=list(data) if isinstance(data, dict) else None)
        return data
    if resp.status_code == 404:
        log.info("trucks_primary_return_none_404", device_id=device_id)
        return None
    log.warning("trucks_primary_return_none_status", device_id=device_id, status=resp.status_code)
    return None


async def _secondary_ulip(state: GatewayState, device_id: str) -> Optional[dict]:
    """SECONDARY: ULIP relay GPS via our own /api/ulip/proxy (mock if no key)."""
    cfg = state.cfg
    url = f"http://127.0.0.1:{cfg.port}/api/ulip/proxy/{device_id}"
    try:
        resp = await state.http.get(url)
    except httpx.HTTPError as exc:
        log.debug("trucks_secondary_unreachable", error=str(exc))
        return None
    if resp.status_code == 200:
        return resp.json()
    return None


async def _list_secondary_rds(state: GatewayState, limit: int) -> Optional[list[dict]]:
    """SECONDARY for the fleet LIST: the persisted telemetry tail from RDS.

    ``core.truck_telemetry`` is where the trucking app's MQTT position stream
    lands, so the latest row per device is the SAME observation the truck-sim
    control plane would have served — read from the durable tail instead of the
    sim's memory. This is not a synthesised fleet: a device only appears here
    because it actually reported a position.

    Returns None when the DSN is unset or the query fails, so the caller can fall
    through to the next rung. Ordered newest-device-first and capped by ``limit``.
    """
    dsn = getattr(state.cfg, "postgres_dsn", None)
    if not dsn:
        return None
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT DISTINCT ON (device_id)
                   device_id, plate, lat, lon, speed_kmh, heading, ts
            FROM core.truck_telemetry
            WHERE ts > now() - interval '24 hours'
            ORDER BY device_id, ts DESC
            LIMIT :limit
            """,
            {"limit": limit}, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 — a dead rung must fall through
        log.warning("trucks_list_secondary_failed", error=str(exc))
        return None
    devices = []
    for r in rows:
        d = dict(r)
        ts = d.pop("ts", None)
        devices.append({
            "device_id": d.get("device_id"), "plate": d.get("plate"),
            "lat": d.get("lat"), "lon": d.get("lon"),
            "speed_kmh": d.get("speed_kmh"), "heading": d.get("heading"),
            "last_seen": ts.isoformat() if isinstance(ts, datetime) else ts,
            # The telemetry tail carries no TruckState — say so rather than
            # inventing one. See the state-filter note in list_trucks().
            "state": None,
            "source": "rds-telemetry",
        })
    return devices or None


def _list_tertiary_checkins(limit: int) -> Optional[list[dict]]:
    """TERTIARY for the fleet LIST: the manual web check-ins already held in
    memory — the same rung ``GET /api/trucks/{id}`` falls back to. A vehicle here
    is admitted under elevated scrutiny (see the module docstring)."""
    if not CHECKINS:
        return None
    devices = []
    for device_id, rec in list(CHECKINS.items())[:limit]:
        devices.append({
            "device_id": device_id, "plate": rec.get("plate"),
            "lat": rec.get("lat"), "lon": rec.get("lon"),
            "speed_kmh": None, "heading": None,
            "last_seen": rec.get("submitted_at"),
            "state": None,
            "source": "web-checkin", "elevated_scrutiny": True,
        })
    return devices or None


@router.get("")
@router.get("/")
async def list_trucks(
    state: str | None = Query(default=None, description="filter to one TruckState"),
    limit: int = Query(default=200, ge=1, le=2000),
    gw: GatewayState = Depends(get_state),
) -> dict:
    """Sampled list of live trucks for the dashboard.

    ``state=AT_GATE_QUEUE`` powers the Driver-Advisory queue. Each device carries
    ``eta_s`` and ``remaining_km`` so the dashboard can render ETA-to-gate
    without a second round-trip.

    FALLBACK CHAIN. This endpoint used to proxy the truck-sim and nothing else:
    a sim outage returned ``{count: 0, devices: [], degraded: true}``, so the
    whole fleet vanished from the dashboard even though the position stream was
    sitting in RDS. It now follows the SAME ladder the single-device read below
    already implements:

        PRIMARY   -> truck-sim /devices/list      (live control plane)
        CACHED    -> last good payload, in-process (sim blip; marked with age)
        SECONDARY -> core.truck_telemetry          (persisted position tail)
        TERTIARY  -> in-memory /checkin submissions (elevated scrutiny)

    STATE FILTER. Only the PRIMARY rung knows a device's ``TruckState``. When a
    caller asks for one (``state=AT_GATE_QUEUE``) and the sim is unavailable, the
    CACHED rung still answers it (the memo was recorded state-filtered, and says
    how old it is); past the memo the remaining rungs are SKIPPED and the
    response degrades to empty — returning the unfiltered fleet would answer a
    different question than the one asked. The response says so via
    ``state_filter_supported``.
    """
    url = gw.cfg.truck_api_url.rstrip("/") + "/devices/list"
    params: Dict[str, str] = {"limit": str(limit)}
    if state:
        params["state"] = state
    cache_key = f"{state or 'ALL'}:{limit}"
    # Correlates the trace lines of ONE request across the rungs it walked.
    rid = uuid.uuid4().hex[:12]
    t_req = time.perf_counter()

    # --- CACHED (fresh): a probe from the last ~3 s answers as LIVE would ----
    fresh = _list_cache_get(cache_key, LIST_CACHE_FRESH_S)
    if fresh is not None:
        REQUESTS.labels("trucks", "ok").inc()
        return _trace_list(rid, "CACHED_FRESH", state, limit, fresh[1], t_req)

    # --- PRIMARY: the truck-sim control plane -------------------------------
    t0 = time.perf_counter()
    try:
        resp = await gw.http.get(url, params=params, timeout=TRUCK_LIST_TIMEOUT)
        if resp.status_code == 200:
            await gw.record_decision(
                api="trucks", key="fleet-list", decision_path=TruckPath.PRIMARY.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="truck-sim",
                source_state=SourceState.LIVE)
            REQUESTS.labels("trucks", "ok").inc()
            body = resp.json()
            if isinstance(body, dict):
                body.setdefault("decision_path", TruckPath.PRIMARY.value)
                body.setdefault("source", "truck-sim")
                body.setdefault("degraded", False)
                body.setdefault("state_filter_supported", True)
                _LIST_CACHE[cache_key] = (time.monotonic(), body)
            return _trace_list(rid, "PRIMARY", state, limit, body, t_req)
        log.info("trucks_list_miss", request_id=rid, status=resp.status_code,
                 latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except httpx.HTTPError as exc:
        # The intermittency lives here: a busy truck-sim event loop can answer
        # slower than TRUCK_LIST_TIMEOUT even though it is perfectly healthy.
        # Log the exception CLASS and the elapsed time so a timeout is
        # distinguishable from a refused connection at a glance.
        log.warning("trucks_list_unreachable", request_id=rid, url=url,
                    error=str(exc), error_class=type(exc).__name__,
                    elapsed_ms=round((time.perf_counter() - t0) * 1000, 1))

    # --- CACHED (stale): the last GOOD payload, served marked, not silently --
    stale = _list_cache_get(cache_key, LIST_CACHE_STALE_S)
    if stale is not None:
        age_s, body = stale
        await gw.record_decision(
            api="trucks", key="fleet-list", decision_path=TruckPath.CACHED.value,
            source="memo", source_state=SourceState.DEGRADED,
            detail={"age_s": round(age_s, 1)})
        REQUESTS.labels("trucks", "degraded").inc()
        return _trace_list(rid, "CACHED_STALE", state, limit,
                           {**body, "degraded": True,
                            "decision_path": TruckPath.CACHED.value,
                            "source": "memo", "cache_age_s": round(age_s, 1)}, t_req)

    # A state-filtered query cannot be answered by the rungs below (see above).
    if state:
        REQUESTS.labels("trucks", "degraded").inc()
        return _trace_list(rid, "UNANSWERABLE", state, limit,
                           {"count": 0, "filter_state": state, "devices": [],
                            "degraded": True, "decision_path": None, "source": None,
                            "state_filter_supported": False,
                            "hint": "TruckState is only known to the truck-sim; "
                                    "start it to filter by state."}, t_req)

    # --- SECONDARY: the persisted telemetry tail in RDS ---------------------
    devices = await _list_secondary_rds(gw, limit)
    if devices:
        await gw.record_decision(
            api="trucks", key="fleet-list", decision_path=TruckPath.SECONDARY.value,
            source="rds-telemetry", source_state=SourceState.DEGRADED)
        REQUESTS.labels("trucks", "degraded").inc()
        return {"count": len(devices), "filter_state": None, "devices": devices,
                "degraded": True, "decision_path": TruckPath.SECONDARY.value,
                "source": "rds-telemetry", "state_filter_supported": False}

    # --- TERTIARY: manual web check-ins -------------------------------------
    devices = _list_tertiary_checkins(limit)
    if devices:
        await gw.record_decision(
            api="trucks", key="fleet-list", decision_path=TruckPath.TERTIARY.value,
            source="web-checkin", source_state=SourceState.DEGRADED)
        REQUESTS.labels("trucks", "degraded").inc()
        return {"count": len(devices), "filter_state": None, "devices": devices,
                "degraded": True, "decision_path": TruckPath.TERTIARY.value,
                "source": "web-checkin", "state_filter_supported": False}

    REQUESTS.labels("trucks", "degraded").inc()
    return {"count": 0, "filter_state": state, "devices": [], "degraded": True,
            "decision_path": None, "source": None, "state_filter_supported": False}


@router.post("/{device_id}/route")
async def reroute_truck(
    device_id: str,
    body: Dict[str, object] = Body(default_factory=dict),
    gw: GatewayState = Depends(get_state),
) -> dict:
    """Force a new route for a truck (Driver-Advisory "Push Re-route", TFC-3).

    Body forwards straight to the truck-sim: ``{gate_id}`` or ``{lat, lon}`` plus
    an optional ``force_state``. We record the override as a decision so it shows
    up in the demo evidence trail.

    The re-route is then pushed to the driver's PWA on every configured channel
    so it always lands within the 5 s SLA:

      * a ``type=reroute`` WebSocket frame (the PWA's realtime worker filters it
        by ``device_id``) — the live, in-app path; the polling fallback reads the
        same advisory back via ``GET /api/trucks/{id}/route/latest``;
      * a WebPush notification (best-effort; only when VAPID is configured and the
        device has a subscription), so a backgrounded PWA still buzzes;
      * a Firebase FCM message (best-effort; only when Firebase is configured and
        the device has a registered token) — the production push transport.

    All three are fanned out by the notification dispatcher; the client de-dupes
    across them so the driver sees a single banner.

    ORDERING (fix T-1). The truck-sim is an OPTIONAL DOWNSTREAM, not a gate. The
    driver-facing work — persist the advisory, record the decision, push it on
    every channel — happens FIRST and unconditionally; the sim is told afterwards.
    The old order called the sim first and raised 502 on a connect error, which
    dropped the whole advisory on the floor: nothing reached
    ``core.reroute_advisory``, no decision was audited and the driver was never
    notified, purely because a simulator was down. A sim failure now degrades the
    response (``sim.delivered=false`` + ``decision_path="REROUTE_DEGRADED"``)
    instead of failing the workflow.
    """
    # Build the driver-facing advisory from the REQUEST alone, so it never depends
    # on the sim's reply. `dest`/`route_km` are enriched from the sim below when it
    # answers in time (the advisory dict is mutated in place, before the response).
    advisory = {
        "type": "reroute",
        "device_id": device_id,
        "ts": _utcnow_iso(),
        "gate_id": body.get("gate_id"),
        "dest": body.get("dest"),
        "route_km": None,
        "reason": body.get("reason", "Traffic / gate advisory — new gate assigned"),
        "title": "Re-route advisory",
        "body": f"Proceed to {body.get('gate_id') or 'new destination'}.",
        "requires_ack": True,
    }
    LAST_REROUTE[device_id] = advisory
    # Durable first (migration 0115): the advisory survives a gateway restart, a
    # PWA refresh AND a truck-sim outage. The in-memory dict is only a hot cache.
    await _advisory_repo(gw).save(device_id, advisory)

    # Fan out over WebSocket + WebPush + Firebase FCM via the unified dispatcher.
    from .. import notifications

    fanout = await notifications.dispatch(gw, device_id, advisory, ws_type="reroute")
    push_delivered = fanout.webpush

    # SMS advisory channel (APP-3 / SCOPE-IU2): fan the same advisory out over SMS
    # when a phone number is supplied. Uses the env-gated provider seam (no-op by
    # default), so this never depends on a configured SMS account for the demo.
    from ..sms import advisory_to_sms_text, send_sms

    phone = body.get("phone") or body.get("driver_phone")
    sms_result = send_sms(phone, advisory_to_sms_text(advisory)) if phone else None

    # ---- OPTIONAL DOWNSTREAM: tell the truck-sim to actually move the vehicle.
    url = gw.cfg.truck_api_url.rstrip("/") + f"/devices/{device_id}/route"
    data: Dict[str, object] = {}
    sim_ok, sim_error = False, None
    try:
        resp = await gw.http.post(url, json=body, timeout=TRUCK_UPSTREAM_TIMEOUT_S)
        if resp.status_code >= 400:
            sim_error = f"truck_sim_status_{resp.status_code}"
            log.warning("trucks_reroute_sim_rejected", url=url,
                        status=resp.status_code)
        else:
            data = resp.json()
            sim_ok = True
    except httpx.HTTPError as exc:
        sim_error = "truck_sim_unreachable"
        log.warning("trucks_reroute_sim_unreachable", url=url, error=str(exc))

    if sim_ok:
        # Enrich the advisory (and its stored copy) with what only the sim knows.
        advisory["dest"] = data.get("dest") or advisory["dest"]
        advisory["route_km"] = data.get("route_km")
        await _advisory_repo(gw).save(device_id, advisory)

    await gw.record_decision(
        api="trucks", key=device_id,
        decision_path="REROUTE" if sim_ok else "REROUTE_DEGRADED",
        source="truck-sim" if sim_ok else "gateway",
        source_state=SourceState.LIVE if sim_ok else SourceState.DEGRADED,
        detail={"reroute": body, "sim_error": sim_error},
    )

    REQUESTS.labels("trucks", "ok" if sim_ok else "degraded").inc()
    return {
        **data,
        "advisory": advisory,
        "persisted": True,
        "decision_path": "REROUTE" if sim_ok else "REROUTE_DEGRADED",
        "sim": {"delivered": sim_ok, "error": sim_error},
        "push_delivered": push_delivered,
        "dispatch": fanout.as_dict(),
        "sms": {"delivered": sms_result.delivered, "provider": sms_result.provider}
        if sms_result
        else None,
    }


@router.get("/{device_id}/route/latest")
async def latest_reroute(device_id: str, gw: GatewayState = Depends(get_state)) -> dict:
    """Polling fallback for the PWA when WebSocket / WebPush are unavailable.

    Returns the most recent re-route advisory dispatched to ``device_id`` (or
    ``{advisory: null}`` if none). The PWA polls this while its socket is down so
    the re-route banner still appears within the SLA.

    Reads the in-memory cache first, then falls back to ``core.reroute_advisory``
    so an advisory dispatched before the last gateway restart is still returned —
    without the RDS rung a refresh silently lost the banner.
    """
    advisory = LAST_REROUTE.get(device_id)
    source = "memory"
    if advisory is None:
        advisory = await _advisory_repo(gw).latest(device_id)
        if advisory is not None:
            LAST_REROUTE[device_id] = advisory  # re-warm the cache
            source = "rds"
    return {"device_id": device_id, "advisory": advisory,
            "source": source if advisory is not None else None}


@router.post("/{device_id}/route/ack")
async def ack_reroute(
    device_id: str,
    body: Dict[str, object] = Body(default_factory=dict),
    gw: GatewayState = Depends(get_state),
) -> dict:
    """Driver accepted/declined a re-route (PWA "Accept" sends ``state=ACK``).

    Recorded as a decision so the demo evidence trail shows the round-trip
    (push -> driver -> ACK) and broadcast so the control-room dashboard can mark
    the advisory acknowledged.

    VALIDATION (fix T-2). There must be an advisory to acknowledge.
    ``AdvisoryRepository.ack()`` already reports whether a row was updated; this
    route used to discard that and answer ``{"acked": true}`` unconditionally —
    *and* wrote a REROUTE_ACK decision first — so an ACK for a device that was
    never pushed anything fabricated a push -> driver -> ACK round-trip in the
    evidence trail. The ACK is now applied first and a miss is a 404; the decision
    is only recorded for an ACK that landed on a real advisory.
    """
    state_val = str(body.get("state", "ACK")).upper()
    # An advisory counts as ack-able if the RDS row was updated OR the hot cache
    # still holds one for this device (the durable write can be best-effort when
    # no DSN is configured — the ACK must not 404 on an advisory the driver can
    # demonstrably see).
    acked = await _advisory_repo(gw).ack(device_id, state_val)
    if not acked and device_id in LAST_REROUTE:
        acked = True
    if not acked:
        log.info("trucks_ack_no_advisory", device_id=device_id, state=state_val)
        REQUESTS.labels("trucks", "not_found").inc()
        raise HTTPException(
            status_code=404,
            detail={"error": "no_advisory_to_ack", "device_id": device_id,
                    "acked": False,
                    "message": f"No re-route advisory is on record for {device_id}."},
        )
    await gw.record_decision(
        api="trucks", key=device_id, decision_path="REROUTE_ACK", source="truck-app",
        source_state=SourceState.LIVE, detail={"state": state_val},
    )
    if device_id in LAST_REROUTE:
        LAST_REROUTE[device_id] = {**LAST_REROUTE[device_id], "ack_state": state_val}
    ack = {"type": "reroute_ack", "device_id": device_id, "state": state_val,
           "ts": _utcnow_iso()}
    # Addressed: the ACK names one driver, so it goes to the control room and that
    # driver's own sockets only (gateway/ws.py `_wants`). Broadcasting it put one
    # driver's device_id on every other driver's socket.
    await gw.ws.broadcast("reroute_ack", ack, device_id=device_id)
    REQUESTS.labels("trucks", "ok").inc()
    return {"acked": True, "device_id": device_id, "state": state_val}


@router.get("/{device_id}")
async def truck_position(device_id: str, state: GatewayState = Depends(get_state)) -> dict:
    cfg = state.cfg

    # Presenter fault injection: a forced rung suppresses the rungs above it so
    # the chain degrades on demand (APP_GPS -> ULIP_RELAY -> WEB_CHECKIN).
    forced = state.faults.forced("trucks")
    skip_primary = forced in (TruckPath.SECONDARY.value, TruckPath.TERTIARY.value)
    skip_secondary = forced == TruckPath.TERTIARY.value

    # --- PRIMARY ---
    t0 = time.perf_counter()
    data = None if skip_primary else await _primary(state, device_id)
    if data is not None:
        await state.record_decision(
            api="trucks", key=device_id, decision_path=TruckPath.PRIMARY.value,
            latency_ms=(time.perf_counter() - t0) * 1000, source="truck-sim",
            source_state=SourceState.LIVE,
        )
        REQUESTS.labels("trucks", "ok").inc()
        return {"device_id": device_id, "decision_path": TruckPath.PRIMARY.value,
                "gate_boom_delay_s": 0, "elevated_scrutiny": False, "record": data}

    # --- SECONDARY (ULIP relay) — elevated scrutiny ---
    relay = None if skip_secondary else await _secondary_ulip(state, device_id)
    if relay is not None:
        await _raise_elevated(state, device_id, relay.get("plate"), TruckPath.SECONDARY.value)
        REQUESTS.labels("trucks", "ok").inc()
        return {"device_id": device_id, "decision_path": TruckPath.SECONDARY.value,
                "gate_boom_delay_s": cfg.gate_boom_delay_s, "elevated_scrutiny": True,
                "record": relay}

    # --- TERTIARY (web check-in) — elevated scrutiny ---
    checkin = CHECKINS.get(device_id)
    if checkin is not None:
        await _raise_elevated(state, device_id, checkin.get("plate"), TruckPath.TERTIARY.value)
        REQUESTS.labels("trucks", "ok").inc()
        return {"device_id": device_id, "decision_path": TruckPath.TERTIARY.value,
                "gate_boom_delay_s": cfg.gate_boom_delay_s, "elevated_scrutiny": True,
                "record": checkin}

    REQUESTS.labels("trucks", "not_found").inc()
    raise HTTPException(
        status_code=404,
        detail={"error": "no_position", "device_id": device_id,
                "hint": "no live GPS, no ULIP relay, and no /checkin on record"},
    )


async def _raise_elevated(
    state: GatewayState, device_id: str, plate: Optional[str], decision_path: str
) -> None:
    cfg = state.cfg
    alert = build_elevated_scrutiny_alert(
        device_id=device_id, plate=plate, decision_path=decision_path,
        gate_boom_delay_s=cfg.gate_boom_delay_s,
    )
    try:
        await persist_alert(alert, dsn=cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("elevated_alert_persist_failed", device_id=device_id, error=str(exc))
    await state.ws.broadcast("alert", alert.model_dump(mode="json"))
    await state.record_decision(
        api="trucks", key=device_id, decision_path=decision_path, source="truck-sim",
        source_state=SourceState.DEGRADED, ok=False,
        detail={"elevated_scrutiny": True, "gate_boom_delay_s": cfg.gate_boom_delay_s,
                "alert_id": str(alert.id)},
    )
