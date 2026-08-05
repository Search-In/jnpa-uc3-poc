"""/api/jobs, /api/gate, /api/yard, /api/scan — the UC-III lifecycle spine.

Thin routers over :class:`services.container_job.ContainerJobService`. These are
the endpoints the audit found missing entirely:

    POST /api/jobs                          -> assign truck+driver to a container job
    POST /api/jobs/validate                 -> dry-run the assignment pre-conditions
    GET  /api/jobs                          -> list/filter jobs
    GET  /api/jobs/{id}                     -> job + full status history
    POST /api/jobs/{id}/accept|complete|cancel
    GET  /api/cargo-jobs/container/{cn}     -> the assignment for one container
    POST /api/gate/events                   -> record a REAL gate crossing (in/out, BAT lane)
    GET  /api/gate/events                   -> query crossings
    POST /api/yard/movements                -> yard pickup / drop / move
    GET  /api/yard/movements
    GET  /api/scan/machines                 -> scanner master
    GET  /api/scan/status/{container_no}    -> does this box need scanning, where?
    POST /api/scan/events                   -> record a scan verdict

RBAC: all four prefixes are CONTROL_ROOM + CUSTOMS in gateway/auth.py._POLICY,
except the driver-facing job actions which a DRIVER may call for its own job
(enforced in gateway/routers/driver_jobs.py, a separate DRIVER-scoped surface).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from ..metrics import REQUESTS
from services.container_job import ContainerJobService, JobConflict, ValidationFailed

router = APIRouter(tags=["container-job"])

_service: Optional[ContainerJobService] = None


def get_service(request: Request) -> ContainerJobService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = ContainerJobService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def _actor(request: Request) -> tuple[Optional[str], Optional[str]]:
    p = getattr(request.state, "principal", None)
    return (getattr(p, "sub", None), getattr(p, "role", None))


def _fail(exc: ValidationFailed) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                         detail={"error": exc.code, "detail": exc.detail, **exc.extra})


def _conflict(exc: JobConflict) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if exc.code == "job_not_found" else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail={"error": exc.code, "detail": exc.detail})


# --------------------------------------------------------------------- DTOs
class AssignIn(BaseModel):
    container_number: Optional[str] = None
    group_code: Optional[str] = Field(None, description="for empty-by-group jobs with no container")
    vehicle_id: Optional[str] = None
    vehicle_no: Optional[str] = None
    driver_id: Optional[str] = None
    driver_licence: Optional[str] = None
    move_type: str = "IMPORT_PICK"
    document_type: Optional[str] = None
    document_reference: Optional[str] = None
    terminal: Optional[str] = None
    gate: Optional[str] = None
    notes: Optional[str] = None


class CancelIn(BaseModel):
    reason: str


class CompleteIn(BaseModel):
    notes: Optional[str] = None


class GateEventIn(BaseModel):
    event_type: str = Field(..., description="GATE_ARRIVAL | GATE_TXN_START | GATE_IN | GATE_OUT")
    plate: str
    gate_id: Optional[str] = None
    job_id: Optional[int] = None
    container_number: Optional[str] = None
    bat_lane: Optional[str] = Field(None, description="terminal BAT lane, e.g. D391 / B723")
    document_type: Optional[str] = None
    document_reference: Optional[str] = None
    driver_id: Optional[str] = None
    device_id: Optional[str] = None
    ts: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


class MovementIn(BaseModel):
    movement_type: str = Field(..., description="YARD_PICKUP | YARD_DROP | YARD_MOVE")
    job_id: Optional[int] = None
    container_number: Optional[str] = None
    yard_location: Optional[str] = Field(None, description="free format, e.g. 2P08D.1")
    from_location: Optional[str] = None
    vehicle_id: Optional[str] = None
    vehicle_no: Optional[str] = None
    driver_id: Optional[str] = None
    terminal: Optional[str] = None
    occurred_at: Optional[datetime] = None


class ScanIn(BaseModel):
    container_number: str
    result: str = Field(..., description="SCANNED_CLEAN | SCAN_HOLD | SCAN_PENDING | SCAN_SKIPPED")
    machine_code: Optional[str] = None
    job_id: Optional[int] = None
    vehicle_id: Optional[str] = None
    vehicle_no: Optional[str] = None
    igm_no: Optional[int] = None
    remarks: Optional[str] = None
    scanned_at: Optional[datetime] = None


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


# ================================================================ assignments
@router.post("/api/jobs/validate", summary="Dry-run the assignment pre-conditions (no write)")
async def validate_assignment(body: AssignIn, request: Request,
                              svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    try:
        res = await svc.validate_assignment(
            container_number=body.container_number, vehicle_id=body.vehicle_id,
            vehicle_no=body.vehicle_no, driver_id=body.driver_id,
            driver_licence=body.driver_licence, move_type=body.move_type,
            group_code=body.group_code)
    except ValidationFailed as exc:
        raise _fail(exc)
    return {"ok": True, "checks": res["checks"],
            "vehicle": res["vehicle"], "permit": res["permit"]}


@router.post("/api/jobs", status_code=status.HTTP_201_CREATED,
             summary="Assign a truck + driver to a container job (validated)")
async def create_job(body: AssignIn, request: Request,
                     svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        res = await svc.assign(
            container_number=body.container_number, vehicle_id=body.vehicle_id,
            vehicle_no=body.vehicle_no, driver_id=body.driver_id,
            driver_licence=body.driver_licence, move_type=body.move_type,
            group_code=body.group_code, document_type=body.document_type,
            document_reference=body.document_reference, terminal=body.terminal,
            gate=body.gate, notes=body.notes, actor=actor, actor_role=role)
    except ValidationFailed as exc:
        raise _fail(exc)
    except JobConflict as exc:
        raise _conflict(exc)
    REQUESTS.labels("jobs", "ok").inc()
    return res


@router.get("/api/jobs", response_model=Page, summary="List container job assignments")
async def list_jobs(
    response: Response,
    container: Optional[str] = None,
    vehicle_id: Optional[str] = None,
    driver_id: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    open_only: bool = False,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    svc: ContainerJobService = Depends(get_service),
) -> Page:
    filters = {"container_number": container, "vehicle_id": vehicle_id,
               "driver_id": driver_id, "status": status_, "open_only": open_only}
    res = await svc.list_jobs(filters=filters, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(res["total"])
    return Page(**res)


@router.get("/api/jobs/{job_id}", summary="One job with its full status history")
async def get_job(job_id: int, svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    job = await svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "job_not_found", "job_id": job_id})
    return job


@router.post("/api/jobs/{job_id}/accept", summary="Driver/operator accepts the job")
async def accept_job(job_id: int, request: Request,
                     svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        return {"job": await svc.accept(job_id, actor=actor, actor_role=role)}
    except JobConflict as exc:
        raise _conflict(exc)


@router.post("/api/jobs/{job_id}/complete", summary="Complete the job")
async def complete_job(job_id: int, request: Request, body: Optional[CompleteIn] = None,
                       svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        return {"job": await svc.complete(job_id, actor=actor, actor_role=role,
                                          notes=(body.notes if body else None))}
    except JobConflict as exc:
        raise _conflict(exc)


@router.post("/api/jobs/{job_id}/cancel", summary="Cancel the job")
async def cancel_job(job_id: int, body: CancelIn, request: Request,
                     svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        return {"job": await svc.cancel(job_id, reason=body.reason, actor=actor, actor_role=role)}
    except JobConflict as exc:
        raise _conflict(exc)


@router.get("/api/cargo-jobs/container/{container_no}",
            summary="The current/latest job assignment for one container")
async def job_for_container(container_no: str,
                            svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    job = await svc.assignment_for_container(container_no)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "assignment_not_found",
                                    "container_number": container_no.strip().upper()})
    return job


# ================================================================ gate events
_GATE_EVENT_TYPES = ("GATE_ARRIVAL", "GATE_TXN_START", "GATE_IN", "GATE_OUT")


@router.post("/api/gate/events", status_code=status.HTTP_201_CREATED,
             summary="Record a real gate crossing (gate-in / gate-out, BAT lane, document)")
async def create_gate_event(body: GateEventIn, request: Request,
                            svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    if body.event_type not in _GATE_EVENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_event_type",
                                    "allowed": list(_GATE_EVENT_TYPES)})
    actor, role = _actor(request)
    try:
        res = await svc.record_gate_event(
            event_type=body.event_type, plate=body.plate, gate_id=body.gate_id,
            job_id=body.job_id, container_number=body.container_number,
            bat_lane=body.bat_lane, document_type=body.document_type,
            document_reference=body.document_reference, driver_id=body.driver_id,
            device_id=body.device_id, ts=body.ts, lat=body.lat, lon=body.lon,
            actor=actor, actor_role=role)
    except JobConflict as exc:
        raise _conflict(exc)
    REQUESTS.labels("gate_events", "ok").inc()
    return res


@router.get("/api/gate/events", summary="Query recorded gate crossings")
async def list_gate_events(
    plate: Optional[str] = None,
    container: Optional[str] = None,
    job_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=1000),
    svc: ContainerJobService = Depends(get_service),
) -> Dict[str, Any]:
    from services.container_job.service import normalize_plate
    items = await svc.gate_events(plate=(normalize_plate(plate) if plate else None),
                                  container_number=(container.strip().upper() if container else None),
                                  job_id=job_id, limit=limit)
    return {"items": items, "count": len(items)}


# ============================================================ yard movements
@router.post("/api/yard/movements", status_code=status.HTTP_201_CREATED,
             summary="Record a yard pickup / drop / move")
async def create_movement(body: MovementIn, request: Request,
                          svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        res = await svc.record_movement(
            movement_type=body.movement_type, job_id=body.job_id,
            container_number=body.container_number, yard_location=body.yard_location,
            from_location=body.from_location, vehicle_id=body.vehicle_id,
            vehicle_no=body.vehicle_no, driver_id=body.driver_id, terminal=body.terminal,
            occurred_at=body.occurred_at, actor=actor, actor_role=role)
    except ValidationFailed as exc:
        raise _fail(exc)
    except JobConflict as exc:
        raise _conflict(exc)
    REQUESTS.labels("yard_movements", "ok").inc()
    return res


@router.get("/api/yard/movements", summary="Query yard movements")
async def list_movements(
    container: Optional[str] = None,
    job_id: Optional[int] = None,
    limit: int = Query(100, ge=1, le=1000),
    svc: ContainerJobService = Depends(get_service),
) -> Dict[str, Any]:
    items = await svc.movements(container_number=(container.strip().upper() if container else None),
                                job_id=job_id, limit=limit)
    return {"items": items, "count": len(items)}


# ================================================================== scanner
@router.get("/api/scan/machines", summary="Scanner machine master (drive-through / mobile / fixed)")
async def list_scanners(active_only: bool = True,
                        svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    items = await svc.scanners(active_only=active_only)
    return {"items": items, "count": len(items)}


@router.get("/api/scan/status/{container_no}",
            summary="Scanner routing for a container: required? which machine? scanned?")
async def scan_status(container_no: str,
                      svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("scan", "ok").inc()
    return await svc.scan_status(container_no)


@router.post("/api/scan/events", status_code=status.HTTP_201_CREATED,
             summary="Record a scan outcome (SCANNED_CLEAN / SCAN_HOLD)")
async def create_scan(body: ScanIn, request: Request,
                      svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        return await svc.record_scan(
            container_number=body.container_number, result=body.result,
            machine_code=body.machine_code, job_id=body.job_id, vehicle_id=body.vehicle_id,
            vehicle_no=body.vehicle_no, igm_no=body.igm_no, remarks=body.remarks,
            scanned_at=body.scanned_at, actor=actor, actor_role=role)
    except ValidationFailed as exc:
        raise _fail(exc)


@router.get("/api/scan/events", summary="Query scan events")
async def list_scans(
    container: Optional[str] = None,
    result: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    svc: ContainerJobService = Depends(get_service),
) -> Dict[str, Any]:
    items = await svc.scans(container_number=(container.strip().upper() if container else None),
                            result=result, limit=limit)
    return {"items": items, "count": len(items)}
