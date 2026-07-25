"""/api/trt — ECY Turn-Round-Time (TRT) KPI (Feature 8).

Measures the full inside-ECY vehicle lifecycle as four ordered phases and rolls
them up into a headline TRT:

    Gate-In -> Parking -> Loading -> Gate-Out  ==>  TRT

Each vehicle advances through the lifecycle via POST /api/trt/phase, which stamps
the matching timestamp column on an OPEN trt_record (created on GATE_IN) and, on
GATE_OUT, computes the per-phase minutes and the total TRT entirely in SQL
(EXTRACT(EPOCH FROM (a-b))/60.0 — the same minute math kpi.py uses over
core.gate_event). RDS-backed (core.trt_record — created in migration
0024_uc3_completion / bootstrapped by gateway.uc3_ext). Additive: no existing
endpoint/table is touched.

    POST /api/trt/phase              -> advance a vehicle a phase (GATE_IN..GATE_OUT)
    GET  /api/trt/records            -> records (filter: status, vehicle_id, limit)
    GET  /api/trt/summary            -> KPI rollup (avg TRT + per-phase, live/baseline)
    GET  /api/trt/vehicle/{id}       -> one vehicle's records
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.trt")

router = APIRouter(prefix="/api/trt", tags=["trt"])

# Lifecycle phases (request) -> the timestamp column each stamps and the resulting
# record status. GATE_OUT closes the record (COMPLETED) and triggers the roll-up.
_PHASES: Dict[str, Dict[str, str]] = {
    "GATE_IN": {"col": "gate_in_at", "status": "GATE_IN"},
    "PARKING": {"col": "parking_at", "status": "PARKED"},
    "LOADING": {"col": "loading_at", "status": "LOADING"},
    "GATE_OUT": {"col": "gate_out_at", "status": "COMPLETED"},
}

# Shown when there is not yet a single COMPLETED record so the dashboard card
# always renders a number (labelled source="baseline", never mistaken for live).
_BASELINE_TRT_MIN = 135.0
_BASELINE_PHASES = {
    "gate_to_park_min": 20.0,
    "park_to_load_min": 75.0,
    "load_to_out_min": 40.0,
}


def _parse_ts(v: Any) -> Any:
    """asyncpg needs a real datetime for a timestamptz bind (CAST won't coerce a
    string). Convert an ISO string to datetime; pass datetimes/None through."""
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return v


def _iso(row: Dict[str, Any]) -> Dict[str, Any]:
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
    return row


@router.post("/phase")
async def advance_phase(body: Dict[str, Any] = Body(...),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Advance a vehicle through the ECY lifecycle.

    Body: {vehicle_id, plate?, trip_id?, phase: GATE_IN|PARKING|LOADING|GATE_OUT, ts?}.

    Finds the latest not-COMPLETED trt_record for the vehicle (or creates one on
    GATE_IN), stamps the matching timestamp column with ``ts`` (or now()) and
    advances the status. On GATE_OUT the per-phase minutes and total TRT are
    computed in SQL and the record is closed COMPLETED, then a ``trt`` WS frame
    is broadcast.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning, fetch_one

    vehicle_id = body.get("vehicle_id")
    if not vehicle_id:
        raise HTTPException(400, "vehicle_id required")
    phase = str(body.get("phase") or "").upper()
    if phase not in _PHASES:
        raise HTTPException(400, f"phase must be one of {sorted(_PHASES)}")
    col = _PHASES[phase]["col"]
    new_status = _PHASES[phase]["status"]
    ts = _parse_ts(body.get("ts"))
    plate = body.get("plate")
    trip_id = body.get("trip_id")

    # Latest still-in-progress record for this vehicle.
    cur = await fetch_one(
        """SELECT * FROM core.trt_record
           WHERE vehicle_id = :vid AND status <> 'COMPLETED'
           ORDER BY created_at DESC LIMIT 1""",
        {"vid": vehicle_id}, dsn=dsn)

    if cur is None:
        if phase != "GATE_IN":
            raise HTTPException(
                409, f"no open TRT record for {vehicle_id}; send GATE_IN first")
        row = await execute_returning(
            """INSERT INTO core.trt_record
                 (vehicle_id, plate, trip_id, gate_in_at, status, source)
               VALUES (:vid, :plate, :trip, COALESCE(CAST(:ts AS timestamptz), now()),
                       'GATE_IN', 'COMPUTED')
               RETURNING *""",
            {"vid": vehicle_id, "plate": plate, "trip": trip_id, "ts": ts}, dsn=dsn)
        if not row:
            raise HTTPException(500, "insert_failed")
        REQUESTS.labels("trt", "ok").inc()
        return {"updated": True, "phase": phase, "record": _iso(dict(row))}

    rid = int(cur["id"])
    if phase == "GATE_OUT":
        # Close the record: stamp gate_out and compute every phase minute + TRT in
        # SQL. Each phase minute is NULL unless both endpoints exist.
        row = await execute_returning(
            f"""UPDATE core.trt_record
                SET {col} = COALESCE(CAST(:ts AS timestamptz), now()),
                    plate = COALESCE(:plate, plate),
                    trip_id = COALESCE(:trip, trip_id),
                    status = 'COMPLETED',
                    gate_to_park_min = CASE
                        WHEN gate_in_at IS NOT NULL AND parking_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (parking_at - gate_in_at)) / 60.0 END,
                    park_to_load_min = CASE
                        WHEN parking_at IS NOT NULL AND loading_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (loading_at - parking_at)) / 60.0 END,
                    load_to_out_min = CASE
                        WHEN loading_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (COALESCE(CAST(:ts AS timestamptz), now()) - loading_at)) / 60.0 END,
                    trt_min = CASE
                        WHEN gate_in_at IS NOT NULL
                        THEN EXTRACT(EPOCH FROM (COALESCE(CAST(:ts AS timestamptz), now()) - gate_in_at)) / 60.0 END,
                    updated_at = now()
                WHERE id = :id
                RETURNING *""",
            {"col": col, "ts": ts, "plate": plate, "trip": trip_id, "id": rid}, dsn=dsn)
    else:
        row = await execute_returning(
            f"""UPDATE core.trt_record
                SET {col} = COALESCE(CAST(:ts AS timestamptz), now()),
                    plate = COALESCE(:plate, plate),
                    trip_id = COALESCE(:trip, trip_id),
                    status = :status, updated_at = now()
                WHERE id = :id
                RETURNING *""",
            {"col": col, "ts": ts, "plate": plate, "trip": trip_id,
             "status": new_status, "id": rid}, dsn=dsn)

    if not row:
        raise HTTPException(500, "update_failed")
    record = _iso(dict(row))

    if phase == "GATE_OUT":
        try:
            await state.ws.broadcast("trt", {
                "type": "trt_completed", "record_id": rid,
                "vehicle_id": vehicle_id, "plate": record.get("plate"),
                "trip_id": record.get("trip_id"), "trt_min": record.get("trt_min"),
                "gate_to_park_min": record.get("gate_to_park_min"),
                "park_to_load_min": record.get("park_to_load_min"),
                "load_to_out_min": record.get("load_to_out_min"),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("trt_ws_failed", error=str(exc))

    REQUESTS.labels("trt", "ok").inc()
    return {"updated": True, "phase": phase, "record": record}


@router.get("/records")
async def list_records(status: Optional[str] = Query(default=None),
                       vehicle_id: Optional[str] = Query(default=None),
                       limit: int = Query(default=100, ge=1, le=1000),
                       state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "records": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    if vehicle_id:
        where.append("vehicle_id = :vid")
        params["vid"] = vehicle_id
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM core.trt_record {clause} ORDER BY created_at DESC LIMIT :limit",
        params, dsn=dsn)
    REQUESTS.labels("trt", "ok").inc()
    return {"count": len(rows), "records": [_iso(dict(r)) for r in rows]}


@router.get("/summary")
async def trt_summary(state: GatewayState = Depends(get_state)) -> dict:
    """KPI rollup over COMPLETED records: avg TRT + avg of each phase.

    ``source`` is ``"live"`` when at least one record has completed, else
    ``"baseline"`` with a labelled placeholder avg so the dashboard card always
    renders a number (mirrors how kpi.py separates live from baseline)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"avg_trt_min": _BASELINE_TRT_MIN, "phases": dict(_BASELINE_PHASES),
                "completed": 0, "open": 0, "source": "baseline"}
    from jnpa_shared.db import fetch_one

    agg = await fetch_one(
        """SELECT
             count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
             count(*) FILTER (WHERE status <> 'COMPLETED') AS open,
             round(avg(trt_min) FILTER (WHERE status = 'COMPLETED')::numeric, 2) AS avg_trt_min,
             round(avg(gate_to_park_min) FILTER (WHERE status = 'COMPLETED')::numeric, 2) AS gate_to_park_min,
             round(avg(park_to_load_min) FILTER (WHERE status = 'COMPLETED')::numeric, 2) AS park_to_load_min,
             round(avg(load_to_out_min) FILTER (WHERE status = 'COMPLETED')::numeric, 2) AS load_to_out_min
           FROM core.trt_record""",
        {}, dsn=dsn)

    completed = int(agg["completed"]) if agg and agg["completed"] is not None else 0
    open_n = int(agg["open"]) if agg and agg["open"] is not None else 0
    REQUESTS.labels("trt", "ok").inc()

    if completed >= 1 and agg is not None and agg["avg_trt_min"] is not None:
        return {
            "avg_trt_min": float(agg["avg_trt_min"]),
            "phases": {
                "gate_to_park_min": float(agg["gate_to_park_min"]) if agg["gate_to_park_min"] is not None else None,
                "park_to_load_min": float(agg["park_to_load_min"]) if agg["park_to_load_min"] is not None else None,
                "load_to_out_min": float(agg["load_to_out_min"]) if agg["load_to_out_min"] is not None else None,
            },
            "completed": completed,
            "open": open_n,
            "source": "live",
        }
    return {
        "avg_trt_min": _BASELINE_TRT_MIN,
        "phases": dict(_BASELINE_PHASES),
        "completed": completed,
        "open": open_n,
        "source": "baseline",
    }


@router.get("/vehicle/{vehicle_id}")
async def vehicle_records(vehicle_id: str,
                          state: GatewayState = Depends(get_state)) -> dict:
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"vehicle_id": vehicle_id, "count": 0, "records": []}
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """SELECT * FROM core.trt_record
           WHERE vehicle_id = :vid ORDER BY created_at DESC""",
        {"vid": vehicle_id}, dsn=dsn)
    REQUESTS.labels("trt", "ok").inc()
    return {"vehicle_id": vehicle_id, "count": len(rows),
            "records": [_iso(dict(r)) for r in rows]}
