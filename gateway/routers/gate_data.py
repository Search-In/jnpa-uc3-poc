"""/api/gate-data — e-seal / Form 13 / weighbridge / ICEGATE -> Auto-LEO
(Appendix C #4, #5).

Proxies the ``gate-data`` service (port 8350); on failure, reconciles in-process
with the service's own deterministic logic so the Auto-LEO panel and Customs feed
always render. The Auto-LEO reconciliation joins the captured records by
container/vehicle, checks e-seal tamper, weighbridge-vs-Form13 weight, and
ICEGATE LEO presence, and raises CUSTOMS_FLAG alerts.

    GET  /api/gate-data/leo/queue       -> reconcile all containers (Auto-LEO feed)
    POST /api/gate-data/leo             -> reconcile one container
    GET  /api/gate-data/customs/flags   -> all open Customs flags + alerts
    GET  /api/gate-data/records/{cn}    -> raw captured records for a container
"""
from __future__ import annotations

import time
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.gate_data")

router = APIRouter(prefix="/api/gate-data", tags=["gate-data"])


async def _upstream(state: GatewayState, method: str, path: str,
                    json: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    url = state.cfg.gate_data_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = await state.http.get(url)
        else:
            resp = await state.http.post(url, json=json or {})
        UPSTREAM_LATENCY.labels("gate-data", "gate-data").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("gate_data_upstream_failed", path=path, error=str(exc))
    return None


def _local():
    from gate_data import leo, seed  # type: ignore
    return leo, seed


def _result_dict(r):
    import dataclasses
    if r is None:
        return None
    if hasattr(r, "to_dict"):
        return r.to_dict()
    if dataclasses.is_dataclass(r):
        return dataclasses.asdict(r)
    return r


@router.get("/leo/queue")
async def leo_queue(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/leo/queue")
    if data is not None:
        REQUESTS.labels("gate-data", "ok").inc()
        return {"decision_path": "LIVE", **data}
    leo, _seed = _local()
    results = [_result_dict(r) for r in leo.reconcile_all()]
    REQUESTS.labels("gate-data", "ok").inc()
    return {"decision_path": "SYNTHETIC", "results": results, "count": len(results)}


@router.post("/leo")
async def leo_one(body: Dict[str, Any] = Body(...),
                  state: GatewayState = Depends(get_state)) -> dict:
    cn = body.get("container_no")
    data = await _upstream(state, "POST", "/leo", {"container_no": cn})
    if data is not None:
        REQUESTS.labels("gate-data", "ok").inc()
        return {"decision_path": "LIVE", **data}
    leo, _seed = _local()
    REQUESTS.labels("gate-data", "ok").inc()
    return {"decision_path": "SYNTHETIC", "result": _result_dict(leo.reconcile(cn))}


@router.get("/customs/flags")
async def customs_flags(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/customs/flags")
    if data is not None:
        REQUESTS.labels("gate-data", "ok").inc()
        return {"decision_path": "LIVE", **data}
    leo, _seed = _local()
    alerts = []
    for r in leo.reconcile_all():
        alerts.extend(leo.customs_alerts(r))
    REQUESTS.labels("gate-data", "ok").inc()
    return {"decision_path": "SYNTHETIC", "alerts": alerts, "count": len(alerts)}


@router.get("/records/{container_no}")
async def records(container_no: str, state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", f"/records/{container_no}")
    if data is not None:
        REQUESTS.labels("gate-data", "ok").inc()
        return {"decision_path": "LIVE", **data}
    _leo, seed = _local()
    rec = seed.generate_dataset().get(container_no)
    REQUESTS.labels("gate-data", "ok").inc()
    return {"decision_path": "SYNTHETIC",
            "container_no": container_no,
            "record": _result_dict(rec) if rec is not None else None}
