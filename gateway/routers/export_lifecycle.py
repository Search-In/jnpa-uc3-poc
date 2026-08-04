"""/api/export — the export container lifecycle (migration 0115).

The audit found the export leg unrepresentable: no booking entity, VGM present
only inside a shipping-line upload parser, COPRAR/COARRI tables seeded with no
API at all, and ``core.cargo.lifecycle_status`` stopping at RELEASED.

    POST /api/export/bookings                      -> 1. liner booking
    POST /api/export/bookings/{id}/form13          -> 2. export gate pass issued
    POST /api/export/bookings/{id}/gate-in         -> 3. truck enters the terminal
    POST /api/export/bookings/{id}/vgm             -> 4. SOLAS verified gross mass
    POST /api/export/bookings/{id}/leo             -> 5. Customs Let Export Order
    POST /api/export/bookings/{id}/load-list       -> 6. COPRAR load list
    POST /api/export/bookings/{id}/loaded          -> 7. COARRI load confirmation
    POST /api/export/bookings/{id}/cancel
    GET  /api/export/bookings[/{id}]               -> list / one booking + history
    GET  /api/export/container/{container_no}      -> the open booking for a box
    GET  /api/export/summary                       -> counts per status

Every step mirrors its state onto ``core.cargo`` and emits on the shared UC-III
lifecycle bus, so an export container appears on the same timeline as an import
one and UC-II sees the same event stream for both legs.

RBAC follows the existing container-job policy (control room + customs write,
everyone authenticated reads) — see gateway/auth.py ``_POLICY``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from services.export_lifecycle import (ExportBookingNotFound, ExportLifecycleService,
                                       ExportTransitionError, ExportValidationError)

from ..metrics import REQUESTS
from ..state import GatewayState, get_state

router = APIRouter(prefix="/api/export", tags=["export-lifecycle"])

_SERVICE: Optional[ExportLifecycleService] = None


def get_service(state: GatewayState = Depends(get_state)) -> ExportLifecycleService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = ExportLifecycleService(state.cfg.postgres_dsn)
    return _SERVICE


def reset_service_for_tests() -> None:
    global _SERVICE
    _SERVICE = None


def _actor(request: Request) -> tuple[Optional[str], Optional[str]]:
    p = getattr(request.state, "principal", None)
    return (getattr(p, "sub", None), getattr(p, "role", None))


def _fail(exc: ExportValidationError) -> HTTPException:
    """A pre-condition the caller can fix -> 409 with the machine-readable code."""
    return HTTPException(status_code=status.HTTP_409_CONFLICT,
                         detail={"error": exc.code, "detail": exc.detail, **exc.extra})


def _not_found(exc: ExportBookingNotFound) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail={"error": "booking_not_found", "booking": str(exc.ref)})


def _illegal(exc: ExportTransitionError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": "illegal_transition", "booking_id": exc.booking_id,
                "current": exc.current, "target": exc.target,
                "detail": f"an export booking in {exc.current} cannot move to {exc.target}"})


# ------------------------------------------------------------------- schemas
class BookingIn(BaseModel):
    booking_no: str = Field(min_length=1, max_length=64)
    container_number: Optional[str] = Field(default=None, max_length=11)
    shipping_line: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_no: Optional[str] = None
    via_no: Optional[str] = None
    pod: Optional[str] = None
    terminal: Optional[str] = None
    cfs_code: Optional[str] = None
    declared_gross_kg: Optional[float] = Field(default=None, ge=0)


class Form13In(BaseModel):
    form13_no: str = Field(min_length=1, max_length=64)
    issued_at: Optional[datetime] = None


class GateInIn(BaseModel):
    gate_id: Optional[str] = None
    truck_no: Optional[str] = None
    job_id: Optional[int] = None
    occurred_at: Optional[datetime] = None


class VgmIn(BaseModel):
    vgm_kg: float = Field(gt=0)
    method: str = Field(default="METHOD_1")
    declared_gross_kg: Optional[float] = Field(default=None, ge=0)
    captured_at: Optional[datetime] = None


class LeoIn(BaseModel):
    leo_no: str = Field(min_length=1, max_length=64)
    shipping_bill_no: Optional[str] = None
    granted_at: Optional[datetime] = None


class LoadListIn(BaseModel):
    coprar_ref: str = Field(min_length=1, max_length=64)
    listed_at: Optional[datetime] = None


class LoadedIn(BaseModel):
    stowage_position: Optional[str] = None
    loaded_at: Optional[datetime] = None


class CancelIn(BaseModel):
    reason: Optional[str] = None


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------- reads
@router.get("/summary", summary="Export lifecycle counts per status")
async def summary(svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("export", "ok").inc()
    return await svc.summary()


@router.get("/bookings", response_model=Page, summary="List export bookings")
async def list_bookings(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    container: Optional[str] = None,
    via_no: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    svc: ExportLifecycleService = Depends(get_service),
) -> Page:
    items, total = await svc.list(
        status=status_filter,
        container_number=(container.strip().upper() if container else None),
        via_no=via_no, limit=limit, offset=offset)
    REQUESTS.labels("export", "ok").inc()
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/bookings/{booking_id}", summary="One booking with its step history")
async def get_booking(booking_id: int,
                      svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    try:
        return await svc.get_with_events(booking_id)
    except ExportBookingNotFound as exc:
        raise _not_found(exc)


@router.get("/container/{container_no}", summary="The open export booking for a container")
async def booking_for_container(
    container_no: str, svc: ExportLifecycleService = Depends(get_service),
) -> Dict[str, Any]:
    row = await svc.for_container(container_no)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "no_open_booking",
                    "container_number": container_no.strip().upper()})
    return row


# --------------------------------------------------------------------- steps
@router.post("/bookings", status_code=status.HTTP_201_CREATED,
             summary="1. Create an export booking")
async def create_booking(body: BookingIn, request: Request,
                         svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, _role = _actor(request)
    try:
        row = await svc.create_booking(**body.model_dump(), created_by=actor)
    except ExportValidationError as exc:
        raise _fail(exc)
    REQUESTS.labels("export", "ok").inc()
    return {"booking": row}


async def _run_step(coro) -> Dict[str, Any]:
    """Shared error mapping for every step endpoint."""
    try:
        return {"booking": await coro}
    except ExportBookingNotFound as exc:
        raise _not_found(exc)
    except ExportTransitionError as exc:
        raise _illegal(exc)
    except ExportValidationError as exc:
        raise _fail(exc)


@router.post("/bookings/{booking_id}/form13", summary="2. Issue the export gate pass")
async def issue_form13(booking_id: int, body: Form13In, request: Request,
                       svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.issue_form13(
        booking_id, form13_no=body.form13_no, issued_at=body.issued_at,
        actor=actor, actor_role=role))


@router.post("/bookings/{booking_id}/gate-in", summary="3. Terminal gate-in on a truck")
async def gate_in(booking_id: int, body: GateInIn, request: Request,
                  svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.gate_in(
        booking_id, gate_id=body.gate_id, truck_no=body.truck_no, job_id=body.job_id,
        occurred_at=body.occurred_at, actor=actor, actor_role=role))


@router.post("/bookings/{booking_id}/vgm", summary="4. Capture SOLAS verified gross mass")
async def capture_vgm(booking_id: int, body: VgmIn, request: Request,
                      svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    try:
        row = await svc.capture_vgm(
            booking_id, vgm_kg=body.vgm_kg, method=body.method,
            declared_gross_kg=body.declared_gross_kg, captured_at=body.captured_at,
            actor=actor, actor_role=role)
    except ExportBookingNotFound as exc:
        raise _not_found(exc)
    except ExportTransitionError as exc:
        raise _illegal(exc)
    except ExportValidationError as exc:
        raise _fail(exc)
    # The variance verdict rides alongside the row so the UI can badge a mismatch
    # without recomputing it (and without the two ever disagreeing).
    return {"booking": row, "vgm": {
        "variance_pct": row.get("vgm_variance_pct"),
        "tolerance_pct": row.get("vgm_tolerance_pct"),
        "flag": row.get("vgm_flag"),
    }}


@router.post("/bookings/{booking_id}/leo", summary="5. Customs Let Export Order")
async def grant_leo(booking_id: int, body: LeoIn, request: Request,
                    svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.grant_leo(
        booking_id, leo_no=body.leo_no, shipping_bill_no=body.shipping_bill_no,
        granted_at=body.granted_at, actor=actor, actor_role=role))


@router.post("/bookings/{booking_id}/load-list", summary="6. Add to the COPRAR load list")
async def add_to_load_list(booking_id: int, body: LoadListIn, request: Request,
                           svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.add_to_load_list(
        booking_id, coprar_ref=body.coprar_ref, listed_at=body.listed_at,
        actor=actor, actor_role=role))


@router.post("/bookings/{booking_id}/loaded", summary="7. COARRI load confirmation")
async def confirm_loaded(booking_id: int, body: LoadedIn, request: Request,
                         svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.confirm_loaded(
        booking_id, stowage_position=body.stowage_position, loaded_at=body.loaded_at,
        actor=actor, actor_role=role))


@router.post("/bookings/{booking_id}/cancel", summary="Cancel an open booking")
async def cancel_booking(booking_id: int, body: CancelIn, request: Request,
                         svc: ExportLifecycleService = Depends(get_service)) -> Dict[str, Any]:
    actor, role = _actor(request)
    return await _run_step(svc.cancel(booking_id, reason=body.reason,
                                      actor=actor, actor_role=role))


__all__ = ["router"]
