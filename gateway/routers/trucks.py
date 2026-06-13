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
from typing import Dict, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..fallback import SourceState, TruckPath
from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..provisional import build_elevated_scrutiny_alert, persist_alert
from ..state import GatewayState, get_state

log = get_logger("gateway.trucks")

router = APIRouter(prefix="/api/trucks", tags=["trucks"])

# Most-recent /checkin submission per device (TERTIARY source). In-memory ring;
# the dashboard reads it back through /api/trucks/{id}. Demo-scale.
CHECKINS: Dict[str, dict] = {}


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


@router.get("/{device_id}")
async def truck_position(device_id: str, state: GatewayState = Depends(get_state)) -> dict:
    cfg = state.cfg

    # --- PRIMARY ---
    t0 = time.perf_counter()
    data = await _primary(state, device_id)
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
    relay = await _secondary_ulip(state, device_id)
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
