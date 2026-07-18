"""/api/cfs-ecy — CFS-ECY CODECO gate movements (UC-III module 13, additive, read-only).

A thin router over :class:`services.cfs_ecy.CfsEcyService` (CfsEcyService →
raw-SQL CfsEcyRepository), in the same mould as gateway/routers/drivers_master.py.
It reads the off-dock gate-movement feed (jnpa.cfs_ecy_movements + the derived
jnpa.v_cfs_ecy_dwell view) and enriches a container timeline with the EXISTING
Container Lifecycle status via a soft, best-effort read of jnpa.cargo. It writes
nothing and touches no existing table — auth / JWT / RBAC / cargo / vehicle /
driver / transporter are all untouched.

    GET /api/cfs-ecy/movements                    -> list + filter/search/paginate
    GET /api/cfs-ecy/stats                         -> KPI aggregates + daily throughput
    GET /api/cfs-ecy/dwell                         -> CFS dwell report
    GET /api/cfs-ecy/containers/{container_number} -> CODECO timeline + dwell + cargo status

RBAC: /api/cfs-ecy is not in gateway/auth.py._POLICY, so it inherits the default
"any authenticated role" rule (read-only). No auth change is required or made.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.cfs_ecy import CfsEcyService

router = APIRouter(prefix="/api/cfs-ecy", tags=["cfs-ecy"])

_service: Optional[CfsEcyService] = None


def get_service(request: Request) -> CfsEcyService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CfsEcyService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- DTOs
class MovementOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Optional[int] = None
    facility_type: Optional[str] = None
    container_number: Optional[str] = None
    iso_valid: Optional[bool] = None
    event_ts: Optional[datetime] = None
    mode: Optional[str] = None
    source: Optional[str] = None
    source_file: Optional[str] = None
    created_at: Optional[datetime] = None


class MovementListResponse(BaseModel):
    items: List[MovementOut]
    total: int
    limit: int
    offset: int
    count: int


class DailyThroughput(BaseModel):
    day: str
    in_count: int
    out_count: int


class StatsOut(BaseModel):
    total_in: int
    total_out: int
    total_events: int
    container_count: int
    active_containers: int
    iso_invalid: int
    average_dwell_hours: Optional[float] = None
    median_dwell_hours: Optional[float] = None
    dwell_count: int
    daily_throughput: List[DailyThroughput]


class DwellItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    container_number: Optional[str] = None
    facility_type: Optional[str] = None
    first_in_ts: Optional[datetime] = None
    last_out_ts: Optional[datetime] = None
    in_events: Optional[int] = None
    out_events: Optional[int] = None
    dwell_hours: Optional[float] = None


class DwellResponse(BaseModel):
    items: List[DwellItem]
    total: int
    limit: int
    offset: int
    count: int
    summary: Dict[str, Any]
    note: str


# ------------------------------------------------------------------- helpers
def _facility(value: Optional[str]) -> Optional[str]:
    """Normalize + validate the facility filter to CFS / ECY (else 400)."""
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("CFS", "ECY"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_facility", "facility": value})
    return v


def _mode(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("IN", "OUT"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_mode", "mode": value})
    return v


def _filters(facility, mode, container, date_from, date_to) -> Dict[str, Any]:
    return {
        "facility_type": _facility(facility),
        "mode": _mode(mode),
        "container": container,
        "ts_from": date_from,
        "ts_to": date_to,
    }


# ------------------------------------------------------------------- endpoints
@router.get("/movements", response_model=MovementListResponse,
            summary="List / search CFS-ECY CODECO gate movements")
async def list_movements(
    facility: Optional[str] = Query(default=None, description="CFS | ECY"),
    mode: Optional[str] = Query(default=None, description="IN | OUT"),
    container: Optional[str] = Query(default=None, description="container number contains"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    sort: str = Query(default="event_ts"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: CfsEcyService = Depends(get_service),
) -> MovementListResponse:
    filters = _filters(facility, mode, container, date_from, date_to)
    res = await service.list_movements(filters, sort=sort, direction=direction,
                                       limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return MovementListResponse(**res)


@router.get("/stats", response_model=StatsOut, summary="CFS-ECY KPI aggregates + daily throughput")
async def stats(
    facility: Optional[str] = Query(default=None, description="CFS | ECY"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    service: CfsEcyService = Depends(get_service),
) -> StatsOut:
    filters = _filters(facility, None, None, date_from, date_to)
    res = await service.stats(filters)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return StatsOut(**res)


@router.get("/dwell", response_model=DwellResponse, summary="CFS dwell report (OUT - IN)")
async def dwell(
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: CfsEcyService = Depends(get_service),
) -> DwellResponse:
    filters = {"ts_from": date_from, "ts_to": date_to}
    res = await service.dwell_report(filters, limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return DwellResponse(**res)


@router.get("/containers/{container_number}",
            summary="CODECO timeline + dwell + cargo lifecycle status for one container")
async def container_timeline(container_number: str,
                             service: CfsEcyService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.container_timeline(container_number)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_found",
                                    "container_number": container_number})
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res
