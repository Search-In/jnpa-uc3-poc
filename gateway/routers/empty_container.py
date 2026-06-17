"""/api/empty — empty-container supply-demand allocation (Appendix C #3).

Proxies the ``empty-container`` service (port 8330) and, when it is unreachable,
falls back to the service's own deterministic optimiser imported in-process — so
the dashboard's Empty-Container board and the TRT-empty KPI always render. The
allocation is the same explainable cost-minimising matcher either way, so LIVE
and fallback agree to the decimal.

    GET /api/empty/allocations  -> probable allocations across ECD/CFS/fleet/line
    GET /api/empty/supply       -> depot stock book
    GET /api/empty/demand       -> open demand book
    GET /api/empty/kpi          -> TRT-empty-from-ECD KPI {value,target,deltaPct,...}
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Depends

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.empty_container")

router = APIRouter(prefix="/api/empty", tags=["empty-container"])


async def _upstream(state: GatewayState, path: str) -> Dict[str, Any] | None:
    """GET the empty-container service; None on any failure (caller falls back)."""
    url = state.cfg.empty_container_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        resp = await state.http.get(url)
        UPSTREAM_LATENCY.labels("empty", "empty-container").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("empty_upstream_failed", path=path, error=str(exc))
    return None


def _local():
    """Import the service's pure functions for the synthetic fallback."""
    from empty_container import optimizer, seed  # type: ignore
    return optimizer, seed


@router.get("/allocations")
async def allocations(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "/allocations")
    if data is not None:
        REQUESTS.labels("empty", "ok").inc()
        return {"decision_path": "LIVE", **data}
    optimizer, seed = _local()
    supply = seed.supply_book()
    demand = seed.demand_book()
    allocs = [a.to_dict() if hasattr(a, "to_dict") else a
              for a in optimizer.allocate(supply, demand)]
    REQUESTS.labels("empty", "ok").inc()
    return {"decision_path": "SYNTHETIC", "allocations": allocs, "count": len(allocs)}


@router.get("/supply")
async def supply(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "/supply")
    if data is not None:
        REQUESTS.labels("empty", "ok").inc()
        return {"decision_path": "LIVE", **data}
    _optimizer, seed = _local()
    REQUESTS.labels("empty", "ok").inc()
    return {"decision_path": "SYNTHETIC",
            "depots": [seed.depot_to_dict(d) for d in seed.supply_book()]}


@router.get("/demand")
async def demand(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "/demand")
    if data is not None:
        REQUESTS.labels("empty", "ok").inc()
        return {"decision_path": "LIVE", **data}
    _optimizer, seed = _local()
    REQUESTS.labels("empty", "ok").inc()
    return {"decision_path": "SYNTHETIC",
            "demand": [seed.demand_to_dict(d) for d in seed.demand_book()]}


@router.get("/kpi")
async def kpi(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "/kpi/trt_empty")
    if data is not None:
        REQUESTS.labels("empty", "ok").inc()
        return {"decision_path": "LIVE", "kpi": data}
    optimizer, seed = _local()
    from jnpa_shared.kpi import compute_kpi
    allocs = optimizer.allocate(seed.supply_book(), seed.demand_book())
    mean_trt = optimizer.mean_est_trt(allocs)
    REQUESTS.labels("empty", "ok").inc()
    return {"decision_path": "SYNTHETIC",
            "kpi": compute_kpi("trt_empty_ecd", mean_trt).to_dict()}
