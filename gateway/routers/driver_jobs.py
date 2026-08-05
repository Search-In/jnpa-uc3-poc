"""/api/driver/jobs — the DRIVER-scoped job surface for the mobile PWA.

A driver sees ONLY its own jobs and may only act on them. Scoping is resolved
from the token's device binding (never from client input), the same rule
gateway/routers/driver.py uses for the self-profile: the DRIVER token carries a
device_id which is the vehicle binding, so the driver's jobs are the jobs of that
vehicle.

    GET  /api/driver/jobs               -> my assigned jobs (open first)
    GET  /api/driver/jobs/{id}          -> one of my jobs + history
    POST /api/driver/jobs/{id}/accept
    POST /api/driver/jobs/{id}/gate-arrival     (reached the gate)
    POST /api/driver/jobs/{id}/pickup           (confirm yard pickup)
    POST /api/driver/jobs/{id}/drop             (confirm yard drop)
    POST /api/driver/jobs/{id}/complete         (trip complete)

RBAC: /api/driver is DRIVER + control room + customs in gateway/auth.py._POLICY.
Ownership is enforced here on top of that, so a DRIVER token can never act on
another driver's job even though the prefix is shared with support roles.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..auth import Role, auth_enabled
from services.container_job import ContainerJobService, JobConflict, ValidationFailed
from services.container_job.service import normalize_plate

from .container_job import get_service

router = APIRouter(prefix="/api/driver/jobs", tags=["driver-jobs"])


class LocationIn(BaseModel):
    yard_location: Optional[str] = None
    gate_id: Optional[str] = None
    notes: Optional[str] = None


def _principal(request: Request):
    return getattr(request.state, "principal", None)


def _is_driver(request: Request) -> bool:
    p = _principal(request)
    return getattr(p, "role", None) == Role.DRIVER.value


async def _scope(request: Request, svc: ContainerJobService) -> Dict[str, Any]:
    """Resolve the caller's job scope.

    DRIVER  -> the vehicle bound to the token's device_id (own jobs only).
    Support -> may pass ?vehicle_id= / ?driver_id= explicitly.

    Auth off (the demo profile, ``AUTH_ENABLED=false``) used to return ``{}``
    here, i.e. UNSCOPED — so on the demo build any PWA could list and act on every
    other driver's jobs over REST, which is the same leak the WebSocket isolation
    fix closed on the push side. The PWA always knows its paired device, so we now
    accept an ``X-Device-Id`` header (or ``?device_id=``) as an UNVERIFIED binding
    when there is no token. It can only ever NARROW what the caller sees — exactly
    the rule gateway/ws.py applies to its ``identify`` frame — and a verified JWT
    binding always wins.
    """
    p = _principal(request)
    if p is not None and auth_enabled():
        if getattr(p, "role", None) == Role.DRIVER.value:
            device = getattr(p, "device_id", None)
            if not device:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail={"error": "driver_token_unbound",
                                            "detail": "token carries no device binding"})
            return {"vehicle_id": device, "vehicle_plate": normalize_plate(device)}
        return {}
    device = (request.headers.get("X-Device-Id")
              or request.query_params.get("device_id") or "").strip()
    if device:
        return {"vehicle_id": device, "vehicle_plate": normalize_plate(device)}
    return {}


def _owns(scope: Dict[str, Any], job: Dict[str, Any]) -> bool:
    if not scope:
        return True
    vid = scope.get("vehicle_id")
    return bool(vid) and (job.get("vehicle_id") == vid
                          or normalize_plate(job.get("vehicle_no")) == scope.get("vehicle_plate"))


async def _own_job(request: Request, svc: ContainerJobService, job_id: int) -> Dict[str, Any]:
    scope = await _scope(request, svc)
    job = await svc.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "job_not_found", "job_id": job_id})
    if not _owns(scope, job):
        # 404 (not 403) so a driver cannot probe for other drivers' job ids.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "job_not_found", "job_id": job_id})
    return job


def _actor(request: Request) -> tuple[Optional[str], Optional[str]]:
    p = _principal(request)
    return (getattr(p, "sub", None), getattr(p, "role", None))


def _conflict(exc: JobConflict) -> HTTPException:
    code = status.HTTP_404_NOT_FOUND if exc.code == "job_not_found" else status.HTTP_409_CONFLICT
    return HTTPException(status_code=code, detail={"error": exc.code, "detail": exc.detail})


# ------------------------------------------------------------------- reads
@router.get("", summary="My assigned jobs (driver PWA)")
async def my_jobs(request: Request,
                  vehicle_id: Optional[str] = None,
                  driver_id: Optional[str] = None,
                  include_closed: bool = False,
                  limit: int = Query(20, ge=1, le=100),
                  svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    scope = await _scope(request, svc)
    filters: Dict[str, Any] = {"open_only": not include_closed}
    if scope.get("vehicle_id"):
        filters["vehicle_id"] = scope["vehicle_id"]
    else:  # support roles / dev
        if vehicle_id:
            filters["vehicle_id"] = vehicle_id
        if driver_id:
            filters["driver_id"] = driver_id
    res = await svc.list_jobs(filters=filters, limit=limit, offset=0)
    return {"items": res["items"], "count": res["count"], "total": res["total"],
            "scope": ("driver" if scope else "support")}


@router.get("/{job_id}", summary="One of my jobs with its history")
async def my_job(job_id: int, request: Request,
                 svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    return await _own_job(request, svc, job_id)


# ------------------------------------------------------------------ actions
@router.post("/{job_id}/accept", summary="Accept the job")
async def accept(job_id: int, request: Request,
                 svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    await _own_job(request, svc, job_id)
    actor, role = _actor(request)
    try:
        return {"job": await svc.accept(job_id, actor=actor, actor_role=role)}
    except JobConflict as exc:
        raise _conflict(exc)


@router.post("/{job_id}/gate-arrival", summary="Reached the terminal gate")
async def gate_arrival(job_id: int, request: Request, body: Optional[LocationIn] = None,
                       svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    job = await _own_job(request, svc, job_id)
    actor, role = _actor(request)
    try:
        return await svc.record_gate_event(
            event_type="GATE_IN", plate=job.get("vehicle_no") or job["vehicle_id"],
            gate_id=(body.gate_id if body else None) or job.get("gate"),
            job_id=job_id, container_number=job.get("container_number"),
            driver_id=job.get("driver_id"), actor=actor, actor_role=role)
    except JobConflict as exc:
        raise _conflict(exc)


@router.post("/{job_id}/pickup", summary="Confirm yard pickup")
async def pickup(job_id: int, request: Request, body: Optional[LocationIn] = None,
                 svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    return await _movement(job_id, request, body, svc, "YARD_PICKUP")


@router.post("/{job_id}/drop", summary="Confirm yard drop")
async def drop(job_id: int, request: Request, body: Optional[LocationIn] = None,
               svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    return await _movement(job_id, request, body, svc, "YARD_DROP")


async def _movement(job_id: int, request: Request, body: Optional[LocationIn],
                    svc: ContainerJobService, movement_type: str) -> Dict[str, Any]:
    job = await _own_job(request, svc, job_id)
    actor, role = _actor(request)
    try:
        return await svc.record_movement(
            movement_type=movement_type, job_id=job_id,
            container_number=job.get("container_number"),
            yard_location=(body.yard_location if body else None),
            vehicle_id=job.get("vehicle_id"), vehicle_no=job.get("vehicle_no"),
            driver_id=job.get("driver_id"), terminal=job.get("terminal"),
            actor=actor, actor_role=role)
    except ValidationFailed as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": exc.code, "detail": exc.detail})
    except JobConflict as exc:
        raise _conflict(exc)


@router.post("/{job_id}/complete", summary="Complete the trip")
async def complete(job_id: int, request: Request, body: Optional[LocationIn] = None,
                   svc: ContainerJobService = Depends(get_service)) -> Dict[str, Any]:
    await _own_job(request, svc, job_id)
    actor, role = _actor(request)
    try:
        return {"job": await svc.complete(job_id, actor=actor, actor_role=role,
                                          notes=(body.notes if body else None))}
    except JobConflict as exc:
        raise _conflict(exc)
