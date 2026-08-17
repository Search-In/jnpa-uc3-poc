"""/api/marine/berth-decisions — the berth allocation decision log.  GAP-FLOW-15.

Flow F-15 asks that re-ordering the berth queue record four things: which call
moved, why, on whose authority and when. Until now the UC-1 planning panel
produced an optimiser proposal, told the planner they could "accept or edit",
and recorded nothing about what they did — so a queue could be re-ordered and
afterwards nobody could reconstruct the decision.

Append-only. Superseding a decision means writing another one; this endpoint has
no PUT and no DELETE, because the value of the log is that it says what was
believed at the time, not what someone later wished had been believed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..datewindow import DateWindow, date_window, window_cond
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.berth_decisions")

router = APIRouter(prefix="/api/marine/berth-decisions", tags=["marine"])


class BerthDecisionIn(BaseModel):
    call_id: str = Field(..., max_length=64,
                         description="VCN or VIA of the call that moved")
    reason_code: str = Field(..., max_length=40)
    vessel_name: Optional[str] = Field(None, max_length=200)
    berth_code: Optional[str] = Field(None, max_length=32)
    from_position: Optional[int] = None
    to_position: Optional[int] = None
    planned_start: Optional[str] = None
    revised_start: Optional[str] = None
    reason_note: Optional[str] = Field(None, max_length=500)
    source: Optional[str] = Field(None, max_length=64,
                                  description="e.g. 'UC-1 AnalyticsPanel'")


@router.get("/reason-codes", summary="The reason vocabulary a planner may pick from")
async def reason_codes(state: GatewayState = Depends(get_state)) -> Dict[str, Any]:
    """Held in a table rather than a CHECK constraint: a berth planner's reasons
    are operational and JNPA will add to them, and a CHECK on a shared database
    would make every addition a migration."""
    from jnpa_shared.db import fetch_all
    try:
        rows = [dict(r) for r in await fetch_all(
            "SELECT code, label, category FROM core.berth_reason_code "
            "WHERE is_active ORDER BY sort_order, code", {},
            dsn=state.cfg.postgres_dsn)]
    except Exception as exc:  # noqa: BLE001
        log.warning("reason_codes_unavailable", error=str(exc))
        rows = []
    REQUESTS.labels("marine", "ok").inc()
    return {"reason_codes": rows, "count": len(rows)}


@router.get("", summary="Berth decisions, newest first")
@router.get("/")
async def list_decisions(
    call_id: Optional[str] = Query(None),
    reason_code: Optional[str] = Query(None),
    window: DateWindow = Depends(date_window),
    limit: int = Query(100, ge=1, le=500),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    from jnpa_shared.db import fetch_all

    conds: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if call_id:
        conds.append("upper(btrim(d.call_id)) = upper(btrim(:call_id))")
        params["call_id"] = call_id
    if reason_code:
        conds.append("d.reason_code = :reason_code")
        params["reason_code"] = reason_code
    cond = window_cond(window, "d.decided_at", params)
    if cond:
        conds.append(cond)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    sql = f"""
        SELECT d.decision_id, d.call_id, d.vessel_name, d.berth_code,
               d.from_position, d.to_position, d.planned_start, d.revised_start,
               d.reason_code, r.label AS reason_label, r.category AS reason_category,
               d.reason_note, d.actor, d.actor_role, d.decided_at,
               d.data_origin, d.source
        FROM core.berth_allocation_decision d
        LEFT JOIN core.berth_reason_code r ON r.code = d.reason_code
        {where}
        ORDER BY d.decided_at DESC
        LIMIT :limit
    """
    try:
        rows = [dict(r) for r in await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)]
    except Exception as exc:  # noqa: BLE001 — an absent table must not 500 the panel
        log.warning("berth_decisions_unavailable", error=str(exc))
        REQUESTS.labels("marine", "error").inc()
        return {"decisions": [], "count": 0, "error": str(exc).splitlines()[0]}

    for r in rows:
        for k in ("planned_start", "revised_start", "decided_at"):
            if hasattr(r.get(k), "isoformat"):
                r[k] = r[k].isoformat()

    REQUESTS.labels("marine", "ok").inc()
    return {"decisions": rows, "count": len(rows), "sql": sql.strip()}


@router.post("", status_code=201, summary="Record one berth decision")
@router.post("/", status_code=201)
async def record_decision(body: BerthDecisionIn, request: Request,
                          state: GatewayState = Depends(get_state)) -> Dict[str, Any]:
    # execute_returning, NOT fetch_all. fetch_all opens a non-transactional
    # `engine.connect()`, so an INSERT through it is rolled back on close while
    # still handing back the new id — a write that reports success and persists
    # nothing. (This helper already existed for exactly that reason; it returns
    # ONE row or None, not a list.)
    from jnpa_shared.db import execute_returning

    known = {r["code"] for r in (await reason_codes(state))["reason_codes"]}
    if known and body.reason_code not in known:
        # Rejected rather than stored: a decision log whose reasons are free text
        # cannot be counted, and "why did we re-order berths this month" is the
        # question it exists to answer.
        raise HTTPException(status_code=400, detail={
            "error": "unknown_reason_code", "reason_code": body.reason_code,
            "known": sorted(known)})

    principal = getattr(request.state, "principal", None)
    actor = getattr(principal, "sub", None) or "unauthenticated"
    actor_role = getattr(principal, "role", None)

    sql = """
        INSERT INTO core.berth_allocation_decision
            (call_id, vessel_name, berth_code, from_position, to_position,
             planned_start, revised_start, reason_code, reason_note,
             actor, actor_role, data_origin, source)
        VALUES
            (:call_id, :vessel_name, :berth_code, :from_position, :to_position,
             CAST(:planned_start AS timestamptz), CAST(:revised_start AS timestamptz),
             :reason_code, :reason_note, :actor, :actor_role, 'MANUAL', :source)
        RETURNING decision_id, decided_at
    """
    params = {**body.model_dump(), "actor": actor, "actor_role": actor_role}
    try:
        row = await execute_returning(sql, params, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # noqa: BLE001
        log.warning("berth_decision_write_failed", error=str(exc))
        raise HTTPException(status_code=503, detail={
            "error": "decision_not_recorded",
            "detail": str(exc).splitlines()[0],
        }) from exc

    row = dict(row) if row else {}
    if hasattr(row.get("decided_at"), "isoformat"):
        row["decided_at"] = row["decided_at"].isoformat()
    REQUESTS.labels("marine", "ok").inc()
    return {"recorded": True, "actor": actor, **row}
