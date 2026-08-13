"""/api/yard — UC-3 peak yard utilisation + truck-arrival management.

ADDITIVE surface. Reuses (never duplicates) the existing modules:

    arrivals   GET /api/trucks?state=AT_GATE_QUEUE   (routers/trucks.list_trucks —
               simulator trucks + registered PWA driver devices, same envelope)
    parking    GET /api/parking/availability          (routers/parking — RDS-backed)
    alerting   services.congestion_alert              (TRAFFIC_CONGESTION, deduped)
    push       gateway.notifications.dispatch         (WS + WebPush + FCM)
    dashboard  state.ws.broadcast                     (live refresh frames)

Endpoints:
    GET  /api/yard/capacity/board                utilisation board (all yards)
    GET  /api/yard/capacity/{yard_id}/events     occupancy audit trail
    POST /api/yard/capacity/{yard_id}/adjust     audited demo/ops occupancy change
    POST /api/yard/capacity/{yard_id}/evaluate   detect constraint -> hold -> notify
    POST /api/yard/capacity/{yard_id}/release    capacity recovered -> release -> notify
    GET  /api/yard/arrivals/holds                arrival-management table (console)
    GET  /api/yard/arrivals/holds/{device_id}    driver-facing hold view (PWA)

Every WRITE sits under the fixed ``/api/yard/capacity`` prefix precisely so the
RBAC overlay can pin it to the control room by prefix (gateway/auth.py
_METHOD_POLICY) without touching the pre-existing ``/api/yard/movements`` job
surface, and without a variable path segment defeating the match.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.yard")

router = APIRouter(prefix="/api/yard", tags=["yard"])

_service = None


def get_service(request: Request):
    """Singleton YardCapacityService wired to the EXISTING gateway seams."""
    global _service
    if _service is None:
        gw: GatewayState = request.app.state.gw
        from services import congestion_alert
        from services.yard_capacity import YardCapacityService, YardThresholds
        from .. import mailer
        from .. import notifications as notif
        from . import parking as parking_router
        from . import trucks as trucks_router

        async def arrivals_fn() -> Dict[str, Any]:
            # The SAME code path the Driver-Advisory console reads — simulator
            # trucks measured AT_GATE_QUEUE plus registered PWA driver devices.
            return await trucks_router.list_trucks(state="AT_GATE_QUEUE",
                                                   limit=500, gw=gw)

        async def parking_fn():
            body = await parking_router.availability(state=gw)
            return body.get("facilities") or []

        async def alert_fn(*, predictions, segment_meta):
            # The EXISTING congestion-alert pipeline: durable core.alert +
            # core.notification, WS broadcast, per-driver WebPush/FCM, admin
            # email — deduped per segment per hour by the service itself.
            from . import push as push_router

            targets = await push_router.registered_devices(gw)

            async def _dispatch(device_id: str, advisory: Dict[str, Any]):
                return await notif.dispatch(gw, device_id, advisory,
                                            ws_type="alert", ws=False)

            return await congestion_alert.raise_congestion_alerts(
                predictions=predictions,
                threshold=gw.cfg.yard_pressure_alert_threshold,
                dsn=gw.cfg.postgres_dsn or None,
                broadcast=gw.ws.broadcast,
                dispatch=_dispatch if targets else None,
                device_targets=targets or None,
                segment_meta=segment_meta,
                email_notify=mailer.notify_congestion_alert,
            )

        async def dispatch_fn(device_id: str, advisory: Dict[str, Any]):
            return await notif.dispatch(gw, device_id, advisory, ws_type="alert")

        _service = YardCapacityService(
            dsn=gw.cfg.postgres_dsn or None,
            thresholds=YardThresholds(
                high_pct=gw.cfg.yard_high_utilization_pct,
                critical_pct=gw.cfg.yard_critical_utilization_pct,
                slots_per_truck=gw.cfg.yard_slots_per_truck,
                release_rate_slots_per_hour=gw.cfg.yard_release_rate_slots_per_hour,
                preferred_facility_id=gw.cfg.yard_preferred_parking_facility,
            ),
            arrivals_fn=arrivals_fn,
            parking_fn=parking_fn,
            alert_fn=alert_fn,
            dispatch_fn=dispatch_fn,
            broadcast_fn=gw.ws.broadcast,
        )
    return _service


def _actor(request: Request) -> Optional[str]:
    p = getattr(request.state, "principal", None)
    for attr in ("username", "subject", "sub", "role"):
        val = getattr(p, attr, None)
        if val:
            return str(val)
    return None


@router.get("/capacity/board")
async def board(yard_id: Optional[str] = Query(None),
                events: int = Query(10, ge=0, le=100),
                svc=Depends(get_service)) -> Dict[str, Any]:
    """Yard utilisation board: utilisation %, capacity, occupied/available slots,
    capacity status per yard — plus the audit tail for the selected yard."""
    try:
        out = await svc.board(yard_id=yard_id, include_events=events)
    except Exception as exc:  # noqa: BLE001 — DB unreachable → explicit, not 500
        log.warning("yard_board_unavailable", error=str(exc))
        REQUESTS.labels("yard", "error").inc()
        return {"yard": None, "yards": [], "recent_events": [], "active_holds": 0,
                "degraded": True, "detail": "yard capacity store unavailable"}
    REQUESTS.labels("yard", "ok").inc()
    return out


@router.get("/arrivals/holds")
async def arrival_holds(yard_id: Optional[str] = Query(None),
                        history: int = Query(25, ge=0, le=200),
                        svc=Depends(get_service)) -> Dict[str, Any]:
    """Arrival-management table for the Congestion Rerouting console."""
    try:
        out = await svc.arrival_board(yard_id=yard_id, include_history=history)
    except Exception as exc:  # noqa: BLE001
        log.warning("yard_holds_unavailable", error=str(exc))
        REQUESTS.labels("yard", "error").inc()
        return {"yard": None, "holds": [], "active_count": 0,
                "released_recent": [], "degraded": True}
    REQUESTS.labels("yard", "ok").inc()
    return out


@router.get("/arrivals/holds/{device_id}")
async def device_hold(device_id: str, svc=Depends(get_service)) -> Dict[str, Any]:
    """This device's current/most-recent arrival hold (PWA polling surface)."""
    try:
        out = await svc.device_hold(device_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("yard_device_hold_unavailable", device_id=device_id, error=str(exc))
        REQUESTS.labels("yard", "error").inc()
        return {"device_id": device_id, "hold": None, "events": [], "degraded": True}
    REQUESTS.labels("yard", "ok").inc()
    return out


@router.get("/capacity/{yard_id}/events")
async def yard_events(yard_id: str, limit: int = Query(25, ge=1, le=200),
                      svc=Depends(get_service)) -> Dict[str, Any]:
    """Occupancy audit trail (core.yard_capacity_event) for one yard."""
    rows = await svc._repo.recent_events(yard_id, limit)  # noqa: SLF001 — read-only
    REQUESTS.labels("yard", "ok").inc()
    from services.yard_capacity.service import _event_view
    return {"yard_id": yard_id, "events": [_event_view(r) for r in rows],
            "count": len(rows)}


@router.post("/capacity/{yard_id}/adjust")
async def adjust(yard_id: str, body: Dict[str, Any] = Body(default_factory=dict),
                 request: Request = None, svc=Depends(get_service)) -> Dict[str, Any]:
    """Audited occupancy change — the demo control.

    Body (one of):
        {"target_utilization_pct": 95}          take the yard to ~95%
        {"delta_slots": 240, "reason": "..."}   add/remove occupied slots
        {"set_occupied": 4560}                  absolute value
    Optional: {"event_type": "INCREASE"|"RELEASE"|"SET", "reason": "..."}.
    Every change lands in core.yard_capacity_event with actor + before/after.
    """
    target = body.get("target_utilization_pct")
    delta = body.get("delta_slots")
    absolute = body.get("set_occupied")
    if target is None and delta is None and absolute is None:
        raise HTTPException(status_code=422, detail={
            "error": "no_change_requested",
            "expected": "target_utilization_pct | delta_slots | set_occupied"})
    event_type = str(body.get("event_type") or "").upper()
    if event_type not in ("INCREASE", "RELEASE", "SET"):
        event_type = ("RELEASE" if (delta is not None and int(delta) < 0)
                      else "INCREASE" if delta is not None else "SET")
    out = await svc.adjust(
        yard_id=yard_id,
        delta_slots=int(delta) if delta is not None else None,
        set_occupied=int(absolute) if absolute is not None else None,
        target_utilization_pct=float(target) if target is not None else None,
        event_type=event_type,
        reason=body.get("reason") or "demo capacity control",
        actor=_actor(request) if request else None,
        detail={"request": {k: body.get(k) for k in
                            ("target_utilization_pct", "delta_slots", "set_occupied")}})
    if out is None:
        REQUESTS.labels("yard", "not_found").inc()
        raise HTTPException(status_code=404,
                            detail={"error": "unknown_yard", "yard_id": yard_id})
    REQUESTS.labels("yard", "ok").inc()
    return out


@router.post("/capacity/{yard_id}/evaluate")
async def evaluate(yard_id: str, body: Dict[str, Any] = Body(default_factory=dict),
                   request: Request = None, svc=Depends(get_service)) -> Dict[str, Any]:
    """Run arrival management now: detect the capacity constraint, raise the
    TRAFFIC_CONGESTION alert, hold the surplus trucks at the recommended
    authorised parking, notify each affected driver. Idempotent — already-held
    trucks are never re-held. ``{"dry_run": true}`` previews without writing."""
    out = await svc.evaluate(
        yard_id=yard_id,
        actor=_actor(request) if request else None,
        notify=bool(body.get("notify", True)),
        dry_run=bool(body.get("dry_run", False)))
    if out.get("error") == "unknown_yard":
        REQUESTS.labels("yard", "not_found").inc()
        raise HTTPException(status_code=404, detail=out)
    REQUESTS.labels("yard", "ok").inc()
    return out


@router.post("/capacity/{yard_id}/release")
async def release(yard_id: str, body: Dict[str, Any] = Body(default_factory=dict),
                  request: Request = None, svc=Depends(get_service)) -> Dict[str, Any]:
    """Release held trucks after capacity recovers.

    Body (all optional):
        {"free_slots": 250}       first frees yard slots (audited RELEASE event),
                                  then releases what the recovered room absorbs
        {"device_ids": [...]}     release these specific trucks
        {"force": true}           operator override — release everything
    Each released driver gets the proceed-to-gate advisory over WS/WebPush/FCM.
    """
    actor = _actor(request) if request else None
    free = body.get("free_slots")
    if free is not None:
        adj = await svc.adjust(yard_id=yard_id, delta_slots=-abs(int(free)),
                               event_type="RELEASE",
                               reason=body.get("reason") or "capacity recovery",
                               actor=actor)
        if adj is None:
            REQUESTS.labels("yard", "not_found").inc()
            raise HTTPException(status_code=404,
                                detail={"error": "unknown_yard", "yard_id": yard_id})
    out = await svc.release(
        yard_id=yard_id,
        device_ids=body.get("device_ids") or None,
        actor=actor,
        notify=bool(body.get("notify", True)),
        force=bool(body.get("force", False)))
    if out.get("error") == "unknown_yard":
        REQUESTS.labels("yard", "not_found").inc()
        raise HTTPException(status_code=404, detail=out)
    REQUESTS.labels("yard", "ok").inc()
    return out


__all__ = ["router"]
