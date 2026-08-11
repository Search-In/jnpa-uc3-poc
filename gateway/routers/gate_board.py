"""/api/gate-board + /api/cpp — T-02 Gate & Lane Board (UC3-021) and
T-05 Parking Plaza metered release (UC3-027).

    GET  /api/gate-board/gates              -> gate cards (in/out, COUNTED queue, avg txn)
    GET  /api/gate-board/lanes              -> lane table (type, state, boom barrier)
    GET  /api/gate-board/ticker             -> confirmed vehicle transactions
    POST /api/gate-board/lanes/{id}/preview -> impact simulation, writes NOTHING
    POST /api/gate-board/lanes/{id}/reassign-> creates a task FOR A HUMAN
    GET  /api/gate-board/tasks              -> reassignment task queue
    POST /api/gate-board/tasks/{id}/ack     -> supervisor acknowledges a task

    GET  /api/cpp/board                     -> occupancy by zone + dwell + amenities
    POST /api/cpp/release/recompute         -> per-terminal metered release (F-06)
    GET  /api/cpp/advice                    -> driver hold-or-proceed advice

Two guarantees are load-bearing and are restated in the payloads themselves:

  * The gate queue is COUNTED from video analytics (core.camera_ai_count) and is
    never inferred from throughput. A gate with no camera observation reports
    ``queue_status = "NO_OBSERVATION"`` rather than a guess, so stopping a gate
    makes the queue rise while throughput reads zero (UI-068).
  * Applying a lane reassignment creates a row in core.lane_reassignment_task and
    returns ``sends_equipment_command: false``. No route here writes to
    core.gate_lane or to any device (UI-103).

RBAC: reads inherit the default "any authenticated role" rule. The three writes
(preview, reassign, ack) are control-room only via the _METHOD_POLICY overlay in
gateway/auth.py — a lane decision is a control-room action, not a driver's.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from ..metrics import REQUESTS
from services.gate_board import GateBoardService
from services.gate_board.repository import LANE_TYPES

router = APIRouter(tags=["gate-board"])

_service: Optional[GateBoardService] = None


def get_service(request: Request) -> GateBoardService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = GateBoardService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def _actor(request: Request) -> Optional[str]:
    """The authenticated principal, for the task's created_by / acknowledged_by."""
    p = getattr(request.state, "principal", None)
    for attr in ("username", "subject", "sub", "role"):
        val = getattr(p, attr, None)
        if val:
            return str(val)
    return None


# ------------------------------------------------------------------ UC3-021
@router.get("/api/gate-board/gates")
async def gate_cards(
    window_minutes: int = Query(60, ge=5, le=1440),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("gate_board", "ok").inc()
    return await svc.gate_cards(window_minutes=window_minutes)


@router.get("/api/gate-board/lanes")
async def lanes(
    gate_id: Optional[str] = Query(None),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("gate_board", "ok").inc()
    return await svc.lanes(gate_id)


@router.get("/api/gate-board/ticker")
async def ticker(
    limit: int = Query(25, ge=1, le=200),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("gate_board", "ok").inc()
    return await svc.ticker(limit)


@router.post("/api/gate-board/lanes/{lane_id}/preview")
async def preview_reassignment(
    lane_id: str,
    body: Dict[str, Any] = Body(default={}),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    """Impact simulation for a proposed reassignment. Writes nothing."""
    to_type = str(body.get("to_lane_type") or "").upper()
    if to_type not in LANE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_lane_type",
                                    "allowed": list(LANE_TYPES)})
    out = await svc.preview_reassignment(lane_id=lane_id, to_type=to_type)
    if out.get("error"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=out)
    REQUESTS.labels("gate_board", "ok").inc()
    return out


@router.post("/api/gate-board/lanes/{lane_id}/reassign")
async def apply_reassignment(
    lane_id: str,
    request: Request,
    body: Dict[str, Any] = Body(default={}),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    """Raise the reassignment as a task for the gate supervisor.

    This endpoint deliberately does NOT change core.gate_lane and issues no
    command to gate equipment (UI-103). The response says so explicitly so a
    client cannot present it as an actuation.
    """
    to_type = str(body.get("to_lane_type") or "").upper()
    if to_type not in LANE_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_lane_type",
                                    "allowed": list(LANE_TYPES)})
    out = await svc.apply_reassignment(lane_id=lane_id, to_type=to_type,
                                       reason=body.get("reason"),
                                       actor=_actor(request))
    if out.get("error"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=out)
    REQUESTS.labels("gate_board", "ok").inc()
    return out


@router.get("/api/gate-board/tasks")
async def tasks(
    task_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=500),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("gate_board", "ok").inc()
    return await svc.tasks(status=task_status, limit=limit)


@router.post("/api/gate-board/tasks/{task_id}/ack")
async def acknowledge_task(
    task_id: str,
    request: Request,
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    row = await svc.acknowledge_task(task_id, _actor(request))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "task_not_found_or_not_pending",
                                    "task_id": task_id})
    REQUESTS.labels("gate_board", "ok").inc()
    return row


@router.get("/api/gate-board/degraded-mode")
async def degraded_mode(
    request: Request,
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    """EC-6 camera-outage ladder for the gate (UC3-023).

    Reads each camera's ACTUAL rung from the ANPR cascade — including any fault
    forced through /api/control/fault — and reports what the gate can honestly
    confirm with the evidence it still has:

        LIVE      ANPR + RFID join            confidence 0.97
        DEGRADED  stale (replayed) ANPR + RFID confidence 0.82
        NO_FEED   RFID only + manual verify   confidence 0.60

    The confidence drop is recorded rather than hidden: a gate that kept
    asserting full confidence after losing half its evidence would pass that
    false certainty to every downstream decision.
    """
    from .anpr import KNOWN_CAMERAS, camera_state

    gw = getattr(request.app.state, "gw", None)
    cameras = [camera_state(gw, cam) for cam in KNOWN_CAMERAS] if gw else []
    REQUESTS.labels("gate_board", "ok").inc()
    return await svc.camera_degraded_mode(cameras)


# ------------------------------------------------------------------ UC3-027
@router.get("/api/cpp/board")
async def cpp_board(svc: GateBoardService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("cpp", "ok").inc()
    return await svc.cpp_board()


@router.post("/api/cpp/release/recompute")
async def recompute_release(
    body: Dict[str, Any] = Body(default={}),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    """Recompute every terminal's release rate from the live counted gate queue.

    ``mode=UNIFORM`` runs the do-nothing comparison (UI-111): one port-wide rate
    for everybody, which visibly degrades the congested terminal.
    """
    mode = str(body.get("mode") or "METERED").upper()
    if mode not in ("METERED", "UNIFORM"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_mode",
                                    "allowed": ["METERED", "UNIFORM"]})
    persist = bool(body.get("persist", True))
    REQUESTS.labels("cpp", "ok").inc()
    return await svc.compute_release_plans(mode=mode, persist=persist)


@router.get("/api/cpp/advice")
async def advice(
    terminal: Optional[str] = Query(None, description="terminal code, e.g. NSICT"),
    svc: GateBoardService = Depends(get_service),
) -> Dict[str, Any]:
    """Driver hold-or-proceed advice, regenerated from the same numbers as the board.

    Reads the latest METERED plan rather than recomputing, so the sentence the
    driver sees is provably the one the control room's board produced.
    """
    out = await svc.compute_release_plans(mode="METERED", persist=False)
    plans = out["plans"]
    if terminal:
        plans = [p for p in plans if p["terminal_code"].upper() == terminal.upper()]
        if not plans:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail={"error": "no_plan_for_terminal",
                                        "terminal": terminal,
                                        "reason": "no counted gate queue for that terminal"})
    REQUESTS.labels("cpp", "ok").inc()
    return {"advice": [{"terminal_code": p["terminal_code"],
                        "text": p["advice_text"],
                        "hold_minutes": p["hold_minutes"],
                        "gate_queue_vehicles": p["gate_queue_vehicles"],
                        "clearing_rate_vph": p["clearing_rate_vph"],
                        "simulated": True} for p in plans],
            "count": len(plans)}
