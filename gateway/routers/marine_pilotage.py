"""/api/marine/pilotage — UC-I Marine pilotage movements (read-only, additive).

A thin router over :class:`services.marine.pilotage.PilotageService`, in the same mould
as gateway/routers/marine_calls.py. Serves the pilot-card movements (INWARD/OUTWARD/
SHIFTING) ingested through the SHARED marine upload endpoints (/api/marine/validate,
/upload, /uploads) — there is NO separate pilotage upload endpoint.

    GET /api/marine/pilotage          -> list + filter/paginate movements
    GET /api/marine/pilotage/stats    -> counts by movement type + distinct pilots
    GET /api/marine/pilotage/{id}     -> one movement

Reads ONLY core.pilotage. RBAC: covered by the existing ("/api/marine", …) policy.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.pilotage import PilotageService

router = APIRouter(prefix="/api/marine/pilotage", tags=["marine"])

_API = "marine_pilotage"
_service: Optional[PilotageService] = None


def get_service(request: Request) -> PilotageService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = PilotageService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class PilotageOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pilotage_id: Optional[int] = None
    movement_type: Optional[str] = None
    call_id: Optional[int] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    pilot_code: Optional[str] = None
    vessel_condition: Optional[str] = None
    from_berth_id: Optional[int] = None
    to_berth_id: Optional[int] = None
    draft_fwd_m: Optional[float] = None
    draft_aft_m: Optional[float] = None
    pilot_boarded_at: Optional[datetime] = None
    first_line_at: Optional[datetime] = None
    all_fast_at: Optional[datetime] = None
    pilot_disembarked_at: Optional[datetime] = None
    berth_vacated_at: Optional[datetime] = None
    anchor_down_at: Optional[datetime] = None
    anchor_up_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None
    extras: Optional[Dict[str, Any]] = None
    import_file_id: Optional[int] = None


class PilotageListResponse(BaseModel):
    items: List[PilotageOut]
    total: int
    limit: int
    offset: int
    count: int


class MovementStat(BaseModel):
    movement_type: Optional[str] = None
    count: int


class PilotageStatsOut(BaseModel):
    total: int
    pilots: int
    by_movement: List[MovementStat]


def _filters(movement, imo, pilot, vessel, via) -> Dict[str, Any]:
    return {"movement_type": movement, "imo_no": imo, "pilot_code": pilot,
            "vessel": vessel, "via": via}


@router.get("", response_model=PilotageListResponse,
            summary="List / filter UC-I pilotage movements")
async def list_pilotage(
    movement: Optional[str] = Query(default=None, description="INWARD | OUTWARD | SHIFTING"),
    imo: Optional[str] = Query(default=None, alias="imo_no"),
    pilot: Optional[str] = Query(default=None, alias="pilot_code"),
    vessel: Optional[str] = Query(default=None, description="vessel name contains"),
    via: Optional[str] = Query(default=None, description="VIA contains"),
    sort: str = Query(default="submitted_at"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PilotageService = Depends(get_service),
) -> PilotageListResponse:
    res = await service.list_pilotage(_filters(movement, imo, pilot, vessel, via),
                                      sort=sort, direction=direction, limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return PilotageListResponse(**res)


@router.get("/stats", response_model=PilotageStatsOut,
            summary="Pilotage counts by movement type + distinct pilots")
async def stats(
    movement: Optional[str] = Query(default=None),
    service: PilotageService = Depends(get_service),
) -> PilotageStatsOut:
    res = await service.stats(_filters(movement, None, None, None, None))
    REQUESTS.labels(_API, "ok").inc()
    return PilotageStatsOut(**res)


@router.get("/{pilotage_id}", response_model=PilotageOut, summary="One pilotage movement")
async def get_pilotage(pilotage_id: int,
                       service: PilotageService = Depends(get_service)) -> PilotageOut:
    res = await service.get(pilotage_id)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "pilotage_not_found", "pilotage_id": pilotage_id})
    REQUESTS.labels(_API, "ok").inc()
    return PilotageOut(**res)
