"""/api/parking — live parking availability inside the geo-fenced port
(Appendix C #1, parking half).

Proxies the ``parking`` service (port 8370). The board is **RDS-backed only**:
occupancy is computed from real slot state in core.parking_slot /
parking_transactions — there is NO synthetic / sine-curve occupancy fallback
(removed in the P0 production-readiness pass). If the upstream service is
unreachable, the gateway reads the same RDS tables directly; only if the
database itself is unavailable does it return ``source="unavailable"`` (empty),
so the dashboard shows an explicit error state instead of fabricated numbers.

    GET /api/parking/availability  -> per-facility capacity/occupied/available (source=rds)
    GET /api/parking/summary       -> board header rollup (source=rds)
    GET /api/parking/facilities    -> facility inventory (geo + capacity, source=rds)
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from urllib.parse import urlencode

from fastapi import APIRouter, Body, Depends, Query

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.parking")

router = APIRouter(prefix="/api/parking", tags=["parking"])


def _loc(location: Any) -> Dict[str, Any]:
    """parking_facilities.location is jsonb — normalise to a dict (it may arrive
    as a dict or a JSON string depending on the driver)."""
    if isinstance(location, dict):
        return location
    if isinstance(location, str):
        try:
            return json.loads(location)
        except Exception:  # noqa: BLE001
            return {}
    return {}


async def _rds_facilities(dsn: Optional[str]) -> List[dict]:
    """Per-facility capacity/occupied/available computed from REAL slot state in
    RDS (mirrors the parking service's persistence.availability query). Never
    fabricates occupancy. Returns [] if the DB is unavailable/unseeded."""
    if not dsn:
        return []
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT f.id AS facility_id, f.facility_name, f.location, f.capacity, f.status,
                   count(s.*) FILTER (WHERE s.availability_status = 'OCCUPIED')  AS occupied,
                   count(s.*) FILTER (WHERE s.availability_status = 'AVAILABLE') AS available
            FROM core.parking_facility f
            LEFT JOIN core.parking_slot s ON s.facility_id = f.id
            GROUP BY f.id, f.facility_name, f.location, f.capacity, f.status
            ORDER BY f.id
            """,
            {},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 - DB unreachable/unseeded → source="unavailable"
        log.debug("parking_rds_unavailable", error=str(exc))
        return []
    board: List[dict] = []
    for r in rows:
        d = dict(r)
        loc = _loc(d.get("location"))
        cap = int(d.get("capacity") or 0)
        occ = int(d.get("occupied") or 0)
        avail = int(d.get("available") or 0)
        board.append({
            "facility_id": d["facility_id"],
            "name": d.get("facility_name"),
            "lat": loc.get("lat"),
            "lon": loc.get("lon"),
            "gate_id": loc.get("gate_id"),
            "capacity": cap,
            "occupied": occ,
            "available": avail,
            "free_pct": round(100.0 * avail / cap, 1) if cap else 0.0,
            "status": "FULL" if avail == 0 and cap > 0 else (d.get("status") or "OPEN"),
        })
    return board


async def _rds_history(dsn: Optional[str], vehicle_id: Optional[str], limit: int) -> Optional[List[dict]]:
    """Entry/exit transactions from RDS (mirrors parking service persistence.history).
    Returns None if the DB is unreachable (caller keeps the empty contract)."""
    if not dsn:
        return None
    from jnpa_shared.db import fetch_all

    where = "WHERE vehicle_id = :vid" if vehicle_id else ""
    params: Dict[str, Any] = {"limit": limit}
    if vehicle_id:
        params["vid"] = vehicle_id
    try:
        rows = await fetch_all(
            f"""
            SELECT id, vehicle_id, driver_id, facility_id, slot_id, entry_time, exit_time,
                   EXTRACT(EPOCH FROM duration) AS duration_s, status
            FROM core.parking_transaction {where}
            ORDER BY entry_time DESC LIMIT :limit
            """,
            params,
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 - DB unreachable → keep empty contract
        log.debug("parking_history_rds_unavailable", error=str(exc))
        return None
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        for k in ("entry_time", "exit_time"):
            if d.get(k) is not None and hasattr(d[k], "isoformat"):
                d[k] = d[k].isoformat()
        if d.get("duration_s") is not None:
            d["duration_s"] = int(d["duration_s"])
        out.append(d)
    return out


async def _rds_violations(dsn: Optional[str], limit: int) -> Optional[List[dict]]:
    """Parking violation / overflow events from RDS (mirrors persistence.violations).
    Returns None if the DB is unreachable (caller keeps the empty contract)."""
    if not dsn:
        return None
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT id, event_type, vehicle_id, facility_id, detail, created_at
            FROM core.parking_event
            WHERE event_type IN ('ILLEGAL_PARKING','NO_PARKING_VIOLATION','OVERFLOW')
            ORDER BY created_at DESC LIMIT :limit
            """,
            {"limit": limit},
            dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001 - DB unreachable → keep empty contract
        log.debug("parking_violations_rds_unavailable", error=str(exc))
        return None
    out: List[dict] = []
    for r in rows:
        d = dict(r)
        d["detail"] = _loc(d.get("detail"))
        if d.get("created_at") is not None and hasattr(d["created_at"], "isoformat"):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    return out


def _rds_summary(board: List[dict]) -> dict:
    return {
        "capacity": sum(int(r["capacity"]) for r in board),
        "occupied": sum(int(r["occupied"]) for r in board),
        "available": sum(int(r["available"]) for r in board),
        "facilities": len(board),
        "full": sum(1 for r in board if r["status"] == "FULL"),
    }


def _summary_contract(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map the RDS/parking-service summary keys to the frontend ParkingSummary
    contract (total_*/full_count). The web ParkingBoard header reads
    total_capacity/total_occupied/total_available/full_count; upstream emits
    capacity/occupied/available/full. Accepts either naming so the header
    populates on the live path, not only against the local mock adapter."""
    return {
        "total_capacity": data.get("total_capacity", data.get("capacity", 0)),
        "total_occupied": data.get("total_occupied", data.get("occupied", 0)),
        "total_available": data.get("total_available", data.get("available", 0)),
        "facilities": data.get("facilities", 0),
        "full_count": data.get("full_count", data.get("full", 0)),
    }


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


async def _upstream_post(state: GatewayState, path: str,
                         json: Dict[str, Any]) -> tuple[int, Dict[str, Any] | None]:
    """POST proxy — returns (status, body). Used for allocate/release/violation."""
    url = state.cfg.parking_url.rstrip("/") + path
    try:
        resp = await state.http.post(url, json=json)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = None
        return resp.status_code, body
    except Exception as exc:  # pragma: no cover
        log.debug("parking_upstream_post_failed", path=path, error=str(exc))
        return 503, None


@router.get("/availability")
async def availability(
    state: GatewayState = Depends(get_state),
) -> dict:
    """Per-facility availability — RDS-backed. Prefers the parking service (which
    reads RDS); on upstream failure reads the RDS slot tables directly. Never
    synthesises occupancy."""
    data = await _upstream(state, "/availability")
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    # Upstream down → read RDS directly (still real slot state).
    board = await _rds_facilities(state.cfg.postgres_dsn)
    if board:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "RDS_DIRECT", "source": "rds", "facilities": board}
    REQUESTS.labels("parking", "error").inc()
    return {"decision_path": "UNAVAILABLE", "source": "unavailable", "facilities": []}


@router.get("/summary")
async def summary(
    state: GatewayState = Depends(get_state),
) -> dict:
    """Board header rollup — RDS-backed (no synthetic occupancy)."""
    data = await _upstream(state, "/summary")
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **_summary_contract(data)}
    board = await _rds_facilities(state.cfg.postgres_dsn)
    if board:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "RDS_DIRECT", "source": "rds",
                **_summary_contract(_rds_summary(board))}
    REQUESTS.labels("parking", "error").inc()
    return {"decision_path": "UNAVAILABLE", "source": "unavailable",
            **_summary_contract({})}


@router.get("/facilities")
async def facilities(state: GatewayState = Depends(get_state)) -> dict:
    """Facility inventory (geo + capacity) — RDS-backed."""
    data = await _upstream(state, "/facilities")
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    board = await _rds_facilities(state.cfg.postgres_dsn)
    if board:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "RDS_DIRECT", "source": "rds", "facilities": board}
    REQUESTS.labels("parking", "error").inc()
    return {"decision_path": "UNAVAILABLE", "source": "unavailable", "facilities": []}


# --- allocation / release / history / violations (RDS-backed via the service) ---
@router.post("/allocate")
async def allocate(body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    """Allocate a parking slot to a vehicle. Body: {facility_id, vehicle_id, driver_id?}."""
    status, data = await _upstream_post(state, "/allocate", body)
    REQUESTS.labels("parking", "ok" if status == 200 else "error").inc()
    return data if data is not None else {"allocated": False, "reason": "service_unavailable"}


@router.post("/release")
async def release(body: Dict[str, Any] = Body(...),
                  state: GatewayState = Depends(get_state)) -> dict:
    """Release a vehicle's parking slot. Body: {vehicle_id}."""
    status, data = await _upstream_post(state, "/release", body)
    REQUESTS.labels("parking", "ok" if status == 200 else "error").inc()
    return data if data is not None else {"released": False, "reason": "service_unavailable"}


@router.post("/violation")
async def violation(body: Dict[str, Any] = Body(...),
                    state: GatewayState = Depends(get_state)) -> dict:
    """Record an illegal-parking / no-parking violation event."""
    status, data = await _upstream_post(state, "/violation", body)
    REQUESTS.labels("parking", "ok" if status == 200 else "error").inc()
    return data if data is not None else {"recorded": False}


@router.get("/history")
async def history(vehicle_id: Optional[str] = Query(default=None),
                  limit: int = Query(default=100, ge=1, le=1000),
                  state: GatewayState = Depends(get_state)) -> dict:
    """Entry/exit transaction history from RDS."""
    q = {k: v for k, v in {"vehicle_id": vehicle_id, "limit": limit}.items() if v is not None}
    data = await _upstream(state, "/history?" + urlencode(q))
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    # Upstream (parking service) down → read the same RDS table directly so the
    # Entry/Exit History and Vehicles tabs survive a parking-service outage.
    txns = await _rds_history(state.cfg.postgres_dsn, vehicle_id, limit)
    if txns is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "RDS_DIRECT", "count": len(txns), "transactions": txns}
    REQUESTS.labels("parking", "error").inc()
    return {"decision_path": "UNAVAILABLE", "count": 0, "transactions": []}


@router.get("/violations")
async def violations(limit: int = Query(default=100, ge=1, le=1000),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Parking violation / overflow events from RDS."""
    data = await _upstream(state, "/violations?" + urlencode({"limit": limit}))
    if data is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "LIVE", **data}
    # Upstream (parking service) down → read core.parking_event directly so the
    # Violations tab never silently shows 0 while the DB actually has rows.
    viols = await _rds_violations(state.cfg.postgres_dsn, limit)
    if viols is not None:
        REQUESTS.labels("parking", "ok").inc()
        return {"decision_path": "RDS_DIRECT", "count": len(viols), "violations": viols}
    REQUESTS.labels("parking", "error").inc()
    return {"decision_path": "UNAVAILABLE", "count": 0, "violations": []}
