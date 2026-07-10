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

# Most-recent /checkin submission per device (TERTIARY source). In-memory ring;
# the dashboard reads it back through /api/trucks/{id}. Demo-scale.
CHECKINS: Dict[str, dict] = {}

# Most-recent re-route advisory dispatched per device — the PWA's polling
# fallback reads this back via GET /api/trucks/{id}/route/latest. Demo-scale.
LAST_REROUTE: Dict[str, dict] = {}


def _utcnow_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def _primary(state: GatewayState, device_id: str) -> Optional[dict]:
    """PRIMARY: the live device snapshot from the trucking-app control plane."""
    cfg = state.cfg
    url = cfg.truck_api_url.rstrip("/") + f"/devices/{device_id}"
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url)
        UPSTREAM_LATENCY.labels("trucks", "truck-sim").observe(time.perf_counter() - t0)
    except httpx.HTTPError as exc:
        log.warning("trucks_primary_unreachable", url=url, error=str(exc))
        return None
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        # Device genuinely unknown to the sim; treat as no PRIMARY (try relay).
        return None
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
        resp = await gw.http.get(url, params=params)
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
        resp = await gw.http.post(url, json=body)
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
async def latest_reroute(device_id: str) -> dict:
    """Polling fallback for the PWA when WebSocket / WebPush are unavailable.

    Returns the most recent re-route advisory dispatched to ``device_id`` (or
    ``{advisory: null}`` if none). The PWA polls this while its socket is down so
    the re-route banner still appears within the SLA.
    """
    return {"device_id": device_id, "advisory": LAST_REROUTE.get(device_id)}


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
    ack = {"type": "reroute_ack", "device_id": device_id, "state": state_val,
           "ts": _utcnow_iso()}
    await gw.ws.broadcast("reroute_ack", ack)
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
