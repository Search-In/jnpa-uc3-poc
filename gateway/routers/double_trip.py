"""/api/double-trip — TT (Twin/Double) Trip Workflow (Feature 15).

Models a vehicle doing two loaded trips in one shift as a *cycle*: Trip-1 ->
Return -> Trip-2, grouped by ``cycle_id``. A "double trip" is a cycle whose two
legs both complete. Provides per-cycle grouping and fleet statistics, plus a
WebSocket ``double_trip`` broadcast when a cycle completes its 2nd leg.
RDS-backed (jnpa.tt_trips, migration 0024). Additive — no existing endpoint/table
is touched.

    POST   /api/double-trip/start            -> start a trip leg (mints/continues a cycle)
    POST   /api/double-trip/{trip_id}/complete-> complete a leg (+broadcast on 2nd)
    GET    /api/double-trip/cycles           -> trips grouped by cycle_id (filter: vehicle_id, limit)
    GET    /api/double-trip/trips            -> flat trip list (filter: vehicle_id, status, limit)
    GET    /api/double-trip/statistics       -> fleet double-trip stats
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.double_trip")

router = APIRouter(prefix="/api/double-trip", tags=["double-trip"])

_DIRECTION = {"INBOUND", "OUTBOUND", "RETURN"}
_STATUS = {"IN_PROGRESS", "COMPLETED", "ABORTED"}


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
        elif isinstance(v, str) and k == "detail":
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


def _now() -> datetime:
    return datetime.now(timezone.utc)


@router.post("/start")
async def start_trip(body: Dict[str, Any] = Body(...),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Start a trip leg. Body: {vehicle_id, driver_id?, origin, destination,
    laden?, cycle_id?, direction?}.

    If ``cycle_id`` is omitted, continue the vehicle's most recent COMPLETED trip
    in an *open* cycle (a cycle with <2 trips); otherwise mint a fresh cycle.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning, fetch_one

    vehicle_id = body.get("vehicle_id")
    if not vehicle_id:
        raise HTTPException(400, "vehicle_id required")
    direction = str(body.get("direction") or "OUTBOUND").upper()
    if direction not in _DIRECTION:
        raise HTTPException(400, f"direction must be one of {sorted(_DIRECTION)}")
    laden = body.get("laden")
    laden = True if laden is None else bool(laden)

    cycle_id = body.get("cycle_id")
    if not cycle_id:
        # Continue the vehicle's most recent COMPLETED trip that sits in an
        # open cycle (fewer than 2 trips), else mint a new cycle id.
        open_cycle = await fetch_one(
            """SELECT cycle_id
                 FROM jnpa.tt_trips
                WHERE vehicle_id = :vid
                GROUP BY cycle_id
               HAVING count(*) < 2
                  AND count(*) FILTER (WHERE status = 'COMPLETED') = count(*)
                ORDER BY max(created_at) DESC
                LIMIT 1""",
            {"vid": vehicle_id}, dsn=dsn)
        if open_cycle and open_cycle.get("cycle_id"):
            cycle_id = open_cycle["cycle_id"]
        else:
            cycle_id = f"TT-{vehicle_id}-{int(_now().timestamp())}"

    # trip_seq = (trips already in this cycle) + 1.
    existing = await fetch_one(
        "SELECT count(*) AS n FROM jnpa.tt_trips WHERE cycle_id = :cid",
        {"cid": cycle_id}, dsn=dsn)
    trip_seq = (int(existing["n"]) if existing else 0) + 1

    row = await execute_returning(
        """INSERT INTO jnpa.tt_trips
             (cycle_id, vehicle_id, driver_id, trip_seq, direction, origin,
              destination, started_at, laden, status, detail)
           VALUES (:cid, :vid, :did, :seq, :dir, :origin, :dest, now(),
              :laden, 'IN_PROGRESS', CAST(:detail AS jsonb))
           RETURNING *""",
        {
            "cid": cycle_id, "vid": vehicle_id, "did": body.get("driver_id"),
            "seq": trip_seq, "dir": direction,
            "origin": body.get("origin"), "dest": body.get("destination"),
            "laden": laden,
            "detail": json.dumps(body.get("detail") or {}),
        },
        dsn=dsn,
    )
    if not row:
        raise HTTPException(500, "insert_failed")
    trip_id = int(row["id"])
    REQUESTS.labels("double_trip", "ok").inc()
    return {"trip_id": trip_id, "cycle_id": cycle_id, "trip_seq": trip_seq,
            "trip": _iso(dict(row))}


@router.post("/{trip_id}/complete")
async def complete_trip(trip_id: int, body: Dict[str, Any] = Body(default={}),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Complete a trip leg. Body: {ended_at?}. Sets ended_at (default now()) and
    status=COMPLETED, then reports whether the cycle is now a completed
    double-trip (2 completed legs) and broadcasts on that 2nd completion."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    from jnpa_shared.db import execute_returning, fetch_one

    cur = await fetch_one("SELECT * FROM jnpa.tt_trips WHERE id = :id",
                          {"id": trip_id}, dsn=dsn)
    if not cur:
        raise HTTPException(404, "trip_not_found")

    # execute_returning (not fetch_one) so the UPDATE actually commits.
    row = await execute_returning(
        """UPDATE jnpa.tt_trips
              SET ended_at = COALESCE(CAST(:ended AS timestamptz), now()),
                  status = 'COMPLETED',
                  updated_at = now()
            WHERE id = :id
        RETURNING *""",
        {"ended": _parse_ts((body or {}).get("ended_at")), "id": trip_id}, dsn=dsn)

    cycle_id = cur["cycle_id"]
    counts = await fetch_one(
        """SELECT count(*) AS n,
                  count(*) FILTER (WHERE status = 'COMPLETED') AS done
             FROM jnpa.tt_trips WHERE cycle_id = :cid""",
        {"cid": cycle_id}, dsn=dsn)
    completed_count = int(counts["done"]) if counts else 0
    trip_count = int(counts["n"]) if counts else 0
    is_double_trip = completed_count >= 2

    # Broadcast only when this completion is the cycle's 2nd completed leg.
    if is_double_trip and completed_count == 2:
        payload = {"type": "double_trip_completed", "cycle_id": cycle_id,
                   "vehicle_id": cur.get("vehicle_id"), "trip_id": trip_id,
                   "trip_count": trip_count,
                   "title": "Double trip completed",
                   "body": f"Vehicle {cur.get('vehicle_id')} completed a double trip"}
        try:
            await state.ws.broadcast("double_trip", payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("double_trip_ws_failed", error=str(exc))

    REQUESTS.labels("double_trip", "ok").inc()
    return {"updated": True, "trip_id": trip_id, "cycle_id": cycle_id,
            "completed_count": completed_count, "trip_count": trip_count,
            "is_double_trip": is_double_trip,
            "trip": _iso(dict(row)) if row else None}


@router.get("/cycles")
async def list_cycles(vehicle_id: Optional[str] = Query(default=None),
                      limit: int = Query(default=50, ge=1, le=500),
                      state: GatewayState = Depends(get_state)) -> dict:
    """Trips grouped by cycle_id (newest first)."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "cycles": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if vehicle_id:
        where.append("vehicle_id = :vid")
        params["vid"] = vehicle_id
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Pull all trips for the most recent `limit` cycles, then group in Python.
    rows = await fetch_all(
        f"""WITH ranked AS (
                SELECT cycle_id, max(created_at) AS last_created
                  FROM jnpa.tt_trips {clause}
                 GROUP BY cycle_id
                 ORDER BY last_created DESC
                 LIMIT :limit)
            SELECT t.*
              FROM jnpa.tt_trips t
              JOIN ranked r ON r.cycle_id = t.cycle_id
             ORDER BY r.last_created DESC, t.cycle_id, t.trip_seq""",
        params, dsn=dsn)

    cycles: List[Dict[str, Any]] = []
    index: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        cid = d["cycle_id"]
        bucket = index.get(cid)
        if bucket is None:
            bucket = {"cycle_id": cid, "vehicle_id": d.get("vehicle_id"),
                      "driver_id": d.get("driver_id"), "trips": []}
            index[cid] = bucket
            cycles.append(bucket)
        bucket["trips"].append(_iso(d))

    for c in cycles:
        trips = c["trips"]
        c["trip_count"] = len(trips)
        c["completed_count"] = sum(1 for t in trips if t.get("status") == "COMPLETED")
        c["is_double_trip"] = c["trip_count"] >= 2
        starts = [t.get("started_at") for t in trips if t.get("started_at")]
        ends = [t.get("ended_at") for t in trips if t.get("ended_at")]
        total_min = None
        if starts and ends:
            try:
                first = min(datetime.fromisoformat(s) for s in starts)
                last = max(datetime.fromisoformat(e) for e in ends)
                total_min = round((last - first).total_seconds() / 60.0, 1)
            except Exception:  # noqa: BLE001
                total_min = None
        c["total_cycle_min"] = total_min

    REQUESTS.labels("double_trip", "ok").inc()
    return {"count": len(cycles), "cycles": cycles}


@router.get("/trips")
async def list_trips(vehicle_id: Optional[str] = Query(default=None),
                     status: Optional[str] = Query(default=None),
                     limit: int = Query(default=100, ge=1, le=1000),
                     state: GatewayState = Depends(get_state)) -> dict:
    """Flat trip list."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"count": 0, "trips": []}
    from jnpa_shared.db import fetch_all

    where: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if vehicle_id:
        where.append("vehicle_id = :vid")
        params["vid"] = vehicle_id
    if status:
        where.append("status = :status")
        params["status"] = status.upper()
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = await fetch_all(
        f"SELECT * FROM jnpa.tt_trips {clause} ORDER BY created_at DESC LIMIT :limit",
        params, dsn=dsn)
    REQUESTS.labels("double_trip", "ok").inc()
    return {"count": len(rows), "trips": [_iso(dict(r)) for r in rows]}


@router.get("/statistics")
async def statistics(state: GatewayState = Depends(get_state)) -> dict:
    """Fleet double-trip statistics over cycles."""
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {
            "source": "baseline", "total_cycles": 0, "double_trip_cycles": 0,
            "double_trip_ratio": 0.0, "avg_trips_per_cycle": 0.0,
            "avg_cycle_min": 0.0, "trips_today": 0, "by_vehicle": [],
        }
    from jnpa_shared.db import fetch_all, fetch_one

    agg = await fetch_one(
        """WITH per_cycle AS (
               SELECT cycle_id,
                      count(*) AS trips,
                      count(*) FILTER (WHERE status = 'COMPLETED') AS completed,
                      min(started_at) AS first_start,
                      max(ended_at) AS last_end
                 FROM jnpa.tt_trips
                GROUP BY cycle_id)
           SELECT
               count(*) AS total_cycles,
               count(*) FILTER (WHERE trips >= 2) AS double_trip_cycles,
               COALESCE(avg(trips), 0) AS avg_trips_per_cycle,
               COALESCE(avg(EXTRACT(EPOCH FROM (last_end - first_start)) / 60.0)
                        FILTER (WHERE completed >= 2 AND last_end IS NOT NULL
                                AND first_start IS NOT NULL), 0) AS avg_cycle_min
             FROM per_cycle""",
        {}, dsn=dsn)

    total_cycles = int(agg["total_cycles"]) if agg else 0
    if total_cycles == 0:
        return {
            "source": "baseline", "total_cycles": 0, "double_trip_cycles": 0,
            "double_trip_ratio": 0.0, "avg_trips_per_cycle": 0.0,
            "avg_cycle_min": 0.0, "trips_today": 0, "by_vehicle": [],
        }

    double_cycles = int(agg["double_trip_cycles"]) if agg else 0
    avg_trips = round(float(agg["avg_trips_per_cycle"]), 2) if agg else 0.0
    avg_cycle_min = round(float(agg["avg_cycle_min"]), 1) if agg else 0.0
    ratio = round(double_cycles / total_cycles, 3) if total_cycles else 0.0

    today = await fetch_one(
        """SELECT count(*) AS n FROM jnpa.tt_trips
            WHERE started_at >= date_trunc('day', now())""",
        {}, dsn=dsn)
    trips_today = int(today["n"]) if today else 0

    by_vehicle_rows = await fetch_all(
        """WITH per_cycle AS (
               SELECT vehicle_id, cycle_id, count(*) AS trips
                 FROM jnpa.tt_trips
                GROUP BY vehicle_id, cycle_id)
           SELECT vehicle_id,
                  count(*) AS cycles,
                  count(*) FILTER (WHERE trips >= 2) AS double_trips
             FROM per_cycle
            GROUP BY vehicle_id
            ORDER BY double_trips DESC, cycles DESC
            LIMIT 10""",
        {}, dsn=dsn)
    by_vehicle = [
        {"vehicle_id": r["vehicle_id"], "cycles": int(r["cycles"]),
         "double_trips": int(r["double_trips"])}
        for r in by_vehicle_rows
    ]

    REQUESTS.labels("double_trip", "ok").inc()
    return {
        "source": "live",
        "total_cycles": total_cycles,
        "double_trip_cycles": double_cycles,
        "double_trip_ratio": ratio,
        "avg_trips_per_cycle": avg_trips,
        "avg_cycle_min": avg_cycle_min,
        "trips_today": trips_today,
        "by_vehicle": by_vehicle,
    }
