"""/api/parking — live parking availability inside the geo-fenced port
(Appendix C #1, parking half).

Proxies the ``parking`` service (port 8370). On failure, computes the
availability snapshot in-process with the service's deterministic occupancy
model so the dashboard's parking-availability board never blanks.

    GET /api/parking/availability  -> per-facility capacity/occupied/available
    GET /api/parking/summary       -> board header rollup
    GET /api/parking/facilities    -> static facility inventory (geo + capacity)
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.parking")

router = APIRouter(prefix="/api/parking", tags=["parking"])


def _minute_of_day(override: Optional[int]) -> int:
    if override is not None:
        return max(0, min(1439, int(override)))
    now = datetime.now()
    return now.hour * 60 + now.minute


async def _upstream(state: GatewayState, path: str) -> Dict[str, Any] | None:
    url = state.cfg.parking_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url)
        UPSTREAM_LATENCY.labels("parking", "parking").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("parking_upstream_failed", path=path, error=str(exc))
    return None


def _local():
    from parking import facilities  # type: ignore
    return facilities


@router.get("/availability")
async def availability(
    minute_of_day: Optional[int] = Query(default=None, ge=0, le=1439),
    state: GatewayState = Depends(get_state),
) -> dict:
    path = "/availability" + (f"?minute_of_day={minute_of_day}" if minute_of_day is not None else "")
    data = await _upstream(state, path)
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    fac = _local()
    mod = _minute_of_day(minute_of_day)
    REQUESTS.labels("parking", "ok").inc()
    return {"decision_path": "SYNTHETIC", "minute_of_day": mod,
            "facilities": fac.snapshot(mod)}


@router.get("/summary")
async def summary(
    minute_of_day: Optional[int] = Query(default=None, ge=0, le=1439),
    state: GatewayState = Depends(get_state),
) -> dict:
    path = "/summary" + (f"?minute_of_day={minute_of_day}" if minute_of_day is not None else "")
    data = await _upstream(state, path)
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    fac = _local()
    mod = _minute_of_day(minute_of_day)
    REQUESTS.labels("parking", "ok").inc()
    return {"decision_path": "SYNTHETIC", **fac.summary(mod)}


@router.get("/facilities")
async def facilities(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "/facilities")
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    fac = _local()
    REQUESTS.labels("parking", "ok").inc()
    return {"decision_path": "SYNTHETIC", "facilities": fac.inventory()}
