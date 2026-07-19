"""/api/drivers/master — Driver Master & Driver Intelligence (UC-III, additive, read-only).

A thin router over :class:`services.driver_master.DriverMasterService`
(DriverMasterService → raw-SQL DriverMasterRepository), in the same mould as
gateway/routers/cargo.py. It reads the Phase-1 registry (jnpa.driver_master +
jnpa.driver_pdp_history) and derives enrollment/verification status by READING
the existing login tables — it never writes to jnpa.drivers / driver_enrollments /
driver_faces / verification_logs / device_bindings, so login, enrollment and
identity are untouched.

    GET /api/drivers/master                       -> list + server-side search/filter/sort/paginate
    GET /api/drivers/master/stats                 -> KPI aggregates
    GET /api/drivers/master/validate/{licence}    -> licence/PDP/enrollment/verification check
    GET /api/drivers/master/{licence}             -> full profile
    GET /api/drivers/master/{licence}/pdp-history -> paginated PDP lineage

RBAC: /api/drivers (customs + admin) — see gateway/auth.py._POLICY (longest-prefix
wins over the DRIVER-scoped /api/driver self-profile rule).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.driver_master import DriverMasterService

router = APIRouter(prefix="/api/drivers/master", tags=["driver-master"])

_service: Optional[DriverMasterService] = None


def get_service(request: Request) -> DriverMasterService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = DriverMasterService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- DTOs
class DriverListItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Optional[int] = None
    licence_no: Optional[str] = None
    name: Optional[str] = None
    company_name: Optional[str] = None
    transporter_id: Optional[int] = None
    transporter_name: Optional[str] = None
    transporter_code: Optional[str] = None
    transporter_status: Optional[str] = None
    photo_file: Optional[str] = None
    photo_url: Optional[str] = None
    licence_type: Optional[str] = None
    licence_valid_to: Optional[date] = None
    latest_pdp_number: Optional[str] = None
    dob: Optional[date] = None
    pdp_status: Optional[str] = None
    pdp_active: Optional[bool] = None
    enrollment_status: Optional[str] = None
    enrolled_driver_id: Optional[str] = None
    driver_status: Optional[str] = None
    verification: Optional[str] = None
    verified_at: Optional[datetime] = None


class DriverListResponse(BaseModel):
    items: List[DriverListItem]
    total: int
    limit: int
    offset: int
    count: int


class DriverStats(BaseModel):
    total_drivers: int
    active_pdp: int
    expiring_soon: int
    expired_pdp: int
    companies: int
    enrolled: int
    pending_enrollment: int
    not_enrolled: int


# ------------------------------------------------------------------- endpoints
@router.get("", response_model=DriverListResponse, summary="List / search the driver registry")
async def list_drivers(
    q: Optional[str] = Query(default=None, description="search licence/name/company/PDP/transporter"),
    company: Optional[str] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status",
                                   description="ACTIVE | EXPIRING | EXPIRED | UNKNOWN"),
    licence: Optional[str] = Query(default=None, description="licence contains (alias of search)"),
    enrolled: Optional[bool] = Query(default=None),
    verification: Optional[str] = Query(default=None, description="VERIFIED | PROVISIONAL | REJECTED"),
    transporter_id: Optional[int] = Query(default=None),
    sort: str = Query(default="name"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    service: DriverMasterService = Depends(get_service),
) -> DriverListResponse:
    filters = {
        "search": (q or licence or None),
        "company": company,
        "status": status_,
        "enrolled": enrolled,
        "verification": verification,
        "transporter_id": transporter_id,
    }
    res = await service.list_drivers(filters, sort=sort, direction=direction,
                                     limit=limit, offset=offset)
    REQUESTS.labels("drivers_master", "ok").inc()
    return DriverListResponse(**res)


@router.get("/stats", response_model=DriverStats, summary="Driver Master KPI aggregates")
async def stats(service: DriverMasterService = Depends(get_service)) -> DriverStats:
    REQUESTS.labels("drivers_master", "ok").inc()
    return DriverStats(**await service.stats())


@router.get("/validate/{licence}", summary="Validate a driver's licence / PDP / enrollment")
async def validate_licence(licence: str,
                           service: DriverMasterService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.validate(licence)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "driver_not_found", "licence": licence})
    REQUESTS.labels("drivers_master", "ok").inc()
    return res


@router.get("/{licence}/pdp-history", summary="Paginated PDP history (lineage) for a driver")
async def pdp_history(licence: str,
                      limit: int = Query(default=25, ge=1, le=200),
                      offset: int = Query(default=0, ge=0),
                      service: DriverMasterService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.get_pdp_history(licence, limit=limit, offset=offset)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "driver_not_found", "licence": licence})
    REQUESTS.labels("drivers_master", "ok").inc()
    return res


@router.get("/{licence}", summary="Full driver profile")
async def driver_profile(licence: str,
                         service: DriverMasterService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.get_profile(licence)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "driver_not_found", "licence": licence})
    REQUESTS.labels("drivers_master", "ok").inc()
    return res
