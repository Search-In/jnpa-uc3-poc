"""/api/carbon — fleet carbon-emissions rollup for the AoI (Appendix C #6).

Proxies the ``carbon`` service (port 8340); on failure, falls back to the
service's own deterministic calculator in-process so the dashboard carbon tile
never blanks. Emission factors are published GHG-Protocol/IPCC-style constants
(see docs/ASSUMPTIONS.md).

    GET  /api/carbon/rollup    -> AoI CO2e total + by-class + moving/idle split
    POST /api/carbon/estimate  -> per-vehicle estimate
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.carbon")

router = APIRouter(prefix="/api/carbon", tags=["carbon"])


async def _upstream(state: GatewayState, method: str, path: str,
                    json: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    url = state.cfg.carbon_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = await state.http.get(url)
        else:
            resp = await state.http.post(url, json=json or {})
        UPSTREAM_LATENCY.labels("carbon", "carbon").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("carbon_upstream_failed", path=path, error=str(exc))
    return None


def _local():
    from carbon import calculator  # type: ignore
    return calculator


@router.get("/rollup")
async def rollup(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/rollup")
    if data is not None:
        REQUESTS.labels("carbon", "ok").inc()
        return {"decision_path": "LIVE", **data}
    calc = _local()
    roll = calc.aoi_rollup(calc.seed_aoi_fleet())
    REQUESTS.labels("carbon", "ok").inc()
    return {"decision_path": "SYNTHETIC", **roll}


@router.post("/estimate")
async def estimate(body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "POST", "/estimate", body)
    if data is not None:
        REQUESTS.labels("carbon", "ok").inc()
        return {"decision_path": "LIVE", **data}
    calc = _local()
    vc = body.get("vehicle_class", "HGV")
    dist = float(body.get("distance_km", 0))
    payload = float(body.get("payload_tonnes", 0))
    idle = float(body.get("idle_minutes", 0))
    REQUESTS.labels("carbon", "ok").inc()
    return {
        "decision_path": "SYNTHETIC",
        "vehicle_class": vc,
        "moving_kg": calc.trip_emissions_kg(dist, payload, vc),
        "idle_kg": calc.idle_emissions_kg(idle, vc),
        "total_kg": calc.vehicle_emissions_kg(dist, payload, idle, vc),
    }
