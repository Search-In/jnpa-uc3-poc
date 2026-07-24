"""/api/ldb — Logistics Data Bank adapter (Feature 13).

Container tracking + movement history over the Logistics Data Bank (LDB). Every
external lookup goes through the shared integration seam
(:mod:`gateway.integrations`) so the LIVE-vs-MOCK posture is explicit and each
call is audited to core.integration_lookup. Movement history is additionally
persisted to core.ldb_movement (migration 0024) so it survives and can be
augmented with manually recorded events.

    GET  /api/ldb/container/{container_number}            -> current tracking
    GET  /api/ldb/container/{container_number}/movements  -> movement history
    POST /api/ldb/movements                               -> record a movement
    GET  /api/ldb/health                                  -> configured / mode flag
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import integrations
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.ldb")

router = APIRouter(prefix="/api/ldb", tags=["ldb"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    """Serialize datetimes to isoformat and decode a stringified detail jsonb."""
    for k, v in list(row.items()):
        if hasattr(v, "isoformat"):
            row[k] = v.isoformat()
        elif isinstance(v, str) and k == "detail":
            try:
                row[k] = json.loads(v)
            except Exception:  # noqa: BLE001
                pass
    return row


# --------------------------------------------------------------- mock builders
def _mock_container(container_number: str) -> Dict[str, Any]:
    """Deterministic LDB current-tracking record keyed off the container number."""
    return {
        "container_number": container_number,
        "status": "IN_TRANSIT",
        "current_location": "JNPA Gate-3",
        "last_event": "GATE_IN",
        "eta": (_now() + timedelta(hours=6)).isoformat(),
        "mode": "ROAD",
    }


def _mock_movements(container_number: str) -> Dict[str, Any]:
    """Deterministic 4-5 step movement chain keyed off the container number."""
    t0 = _now() - timedelta(hours=10)
    steps = [
        ("GATE_IN", "JNPA Gate-3", "NSICT", "ROAD"),
        ("YARD", "NSICT Yard Block-B", "NSICT", "ROAD"),
        ("RAIL_OUT", "JNPT Rail Terminal", "CRT", "RAIL"),
        ("VESSEL_LOAD", "Berth NSICT-2", "NSICT", "VESSEL"),
        ("DEPARTED", "Arabian Sea", "NSICT", "VESSEL"),
    ]
    movements = []
    for i, (event, location, terminal, mode) in enumerate(steps):
        movements.append({
            "ts": (t0 + timedelta(hours=2 * i)).isoformat(),
            "container_number": container_number,
            "event": event,
            "location": location,
            "terminal": terminal,
            "mode": mode,
            "detail": {"seq": i + 1},
        })
    return {"movements": movements}


# --------------------------------------------------------------------- routes
@router.get("/container/{container_number}")
async def ldb_container(container_number: str,
                        state: GatewayState = Depends(get_state)) -> dict:
    """Current tracking status for a container."""
    result = await integrations.call(
        system="LDB", op="container", ref=container_number,
        request={"container_number": container_number},
        live_path=f"/container/{container_number}",
        mock_fn=lambda: _mock_container(container_number),
        dsn=state.cfg.postgres_dsn,
    )
    REQUESTS.labels("ldb", "ok").inc()
    return {"source": result["source"], "tracking": result["data"]}


@router.get("/container/{container_number}/movements")
async def ldb_movements(container_number: str,
                        state: GatewayState = Depends(get_state)) -> dict:
    """Movement history for a container.

    Reads persisted rows from core.ldb_movement first (newest first); if none
    exist, fetches the chain from the LDB adapter and persists each returned
    movement (INSERT with its source) before returning.
    """
    dsn = state.cfg.postgres_dsn

    # 1. Persisted rows first (newest first).
    if dsn:
        from jnpa_shared.db import fetch_all
        rows = await fetch_all(
            """SELECT ts, container_number, event, location, terminal, mode, source, detail
                 FROM core.ldb_movement
                WHERE container_number = :cn
                ORDER BY ts DESC""",
            {"cn": container_number}, dsn=dsn)
        if rows:
            movements = [_iso(dict(r)) for r in rows]
            REQUESTS.labels("ldb", "ok").inc()
            return {"source": "DB", "count": len(movements), "movements": movements}

    # 2. Nothing persisted -> pull from the adapter.
    result = await integrations.call(
        system="LDB", op="movements", ref=container_number,
        request={"container_number": container_number},
        live_path=f"/container/{container_number}/movements",
        mock_fn=lambda: _mock_movements(container_number),
        dsn=dsn,
    )
    movements: List[Dict[str, Any]] = list(result["data"].get("movements") or [])

    # 3. Persist each returned movement (best-effort; degrade when no DSN).
    if dsn and movements:
        from jnpa_shared.db import execute
        for m in movements:
            try:
                await execute(
                    """INSERT INTO core.ldb_movement
                         (ts, container_number, event, location, terminal, mode, source, detail)
                       VALUES (COALESCE(CAST(:ts AS timestamptz), now()), :cn, :event,
                               :location, :terminal, :mode, :source, CAST(:detail AS jsonb))""",
                    {
                        "ts": _parse_ts(m.get("ts")),
                        "cn": container_number,
                        "event": m.get("event"),
                        "location": m.get("location"),
                        "terminal": m.get("terminal"),
                        "mode": m.get("mode"),
                        "source": result["source"],
                        "detail": json.dumps(m.get("detail") or {}),
                    },
                    dsn=dsn)
            except Exception as exc:  # noqa: BLE001 - persistence is best-effort
                log.warning("ldb_movement_persist_failed",
                            container=container_number, error=str(exc))

    REQUESTS.labels("ldb", "ok").inc()
    return {"source": result["source"], "count": len(movements), "movements": movements}


@router.post("/movements")
async def record_movement(body: Dict[str, Any] = Body(...),
                          state: GatewayState = Depends(get_state)) -> dict:
    """Manually record a container movement into core.ldb_movement.

    Body: {container_number, event, location, terminal, mode, detail?}.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        raise HTTPException(503, "database_unavailable")
    container_number = body.get("container_number")
    event = body.get("event")
    if not container_number or not event:
        raise HTTPException(400, "container_number and event are required")

    from jnpa_shared.db import execute_returning
    row = await execute_returning(
        """INSERT INTO core.ldb_movement
             (container_number, event, location, terminal, mode, source, detail)
           VALUES (:cn, :event, :location, :terminal, :mode, 'MANUAL', CAST(:detail AS jsonb))
           RETURNING ts, container_number, event, location, terminal, mode, source, detail""",
        {
            "cn": container_number,
            "event": event,
            "location": body.get("location"),
            "terminal": body.get("terminal"),
            "mode": body.get("mode"),
            "detail": json.dumps(body.get("detail") or {}),
        },
        dsn=dsn)
    if not row:
        raise HTTPException(500, "insert_failed")
    REQUESTS.labels("ldb", "ok").inc()
    return {"recorded": True, "movement": _iso(dict(row))}


@router.get("/health")
async def ldb_health() -> dict:
    """LIVE-vs-MOCK posture for the LDB dependency."""
    return integrations.health("LDB")
