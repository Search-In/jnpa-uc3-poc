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


@router.get("")
@router.get("/")
async def list_trucks(
    state: str | None = Query(default=None, description="filter to one TruckState"),
    limit: int = Query(default=200, ge=1, le=2000),
    gw: GatewayState = Depends(get_state),
) -> dict:
    """Sampled list of live trucks for the dashboard (proxies truck-sim).

    ``state=AT_GATE_QUEUE`` powers the Driver-Advisory queue. Each device carries
    ``eta_s`` and ``remaining_km`` so the dashboard can render ETA-to-gate
    without a second round-trip.
    """
    url = gw.cfg.truck_api_url.rstrip("/") + "/devices/list"
    params: Dict[str, str] = {"limit": str(limit)}
    if state:
        params["state"] = state
    try:
        resp = await gw.http.get(url, params=params, timeout=TRUCK_UPSTREAM_TIMEOUT_S)
        if resp.status_code == 200:
            REQUESTS.labels("trucks", "ok").inc()
            return resp.json()
        log.info("trucks_list_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("trucks_list_unreachable", url=url, error=str(exc))
    REQUESTS.labels("trucks", "degraded").inc()
    return {"count": 0, "filter_state": state, "devices": [], "degraded": True}


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
    """
    url = gw.cfg.truck_api_url.rstrip("/") + f"/devices/{device_id}/route"
    try:
        resp = await gw.http.post(url, json=body, timeout=TRUCK_UPSTREAM_TIMEOUT_S)
    except httpx.HTTPError as exc:
        log.warning("trucks_reroute_unreachable", url=url, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "truck_sim_unreachable"})
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.json())
    data = resp.json()
    await gw.record_decision(
        api="trucks", key=device_id, decision_path="REROUTE", source="truck-sim",
        source_state=SourceState.LIVE, detail={"reroute": body},
    )

    # Build the driver-facing re-route advisory and dispatch it on both channels.
    advisory = {
        "type": "reroute",
        "device_id": device_id,
        "ts": _utcnow_iso(),
        "gate_id": body.get("gate_id"),
        "dest": data.get("dest"),
        "route_km": data.get("route_km"),
        "reason": body.get("reason", "Traffic / gate advisory — new gate assigned"),
        "title": "Re-route advisory",
        "body": f"Proceed to {body.get('gate_id') or 'new destination'}.",
        "requires_ack": True,
    }
    LAST_REROUTE[device_id] = advisory
    # Mirror to RDS (migration 0115) so the advisory survives a gateway restart
    # and a PWA refresh — the in-memory dict above stays as the hot cache.
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

    REQUESTS.labels("trucks", "ok").inc()
    return {
        **data,
        "advisory": advisory,
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
    """
    state_val = str(body.get("state", "ACK")).upper()
    await gw.record_decision(
        api="trucks", key=device_id, decision_path="REROUTE_ACK", source="truck-app",
        source_state=SourceState.LIVE, detail={"state": state_val},
    )
    await _advisory_repo(gw).ack(device_id, state_val)
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
