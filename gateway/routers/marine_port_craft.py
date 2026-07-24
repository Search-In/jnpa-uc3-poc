"""/api/marine/port-craft — UC-I Marine port-craft fleet register (read-only, additive).

A thin router over :class:`services.marine.port_craft.PortCraftService`, in the same
mould as gateway/routers/marine_pilotage.py. Serves the tug/launch register ingested
through the SHARED marine upload endpoints (Details_of_Port_Crafts.pdf) — there is NO
separate port-craft upload endpoint.

    GET /api/marine/port-craft         -> list + filter
    GET /api/marine/port-craft/stats   -> counts by type + ownership
    GET /api/marine/port-craft/{id}    -> one craft

Reads ONLY core.port_craft. RBAC: covered by the existing ("/api/marine", …) policy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.port_craft import PortCraftService

router = APIRouter(prefix="/api/marine/port-craft", tags=["marine"])

_API = "marine_port_craft"
_service: Optional[PortCraftService] = None


def get_service(request: Request) -> PortCraftService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = PortCraftService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class PortCraftOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    craft_id: Optional[int] = None
    name: Optional[str] = None
    craft_type: Optional[str] = None
    owned_or_hired: Optional[str] = None
    owner_name: Optional[str] = None
    year_built: Optional[str] = None
    loa_m: Optional[float] = None
    breadth_m: Optional[float] = None
    draft_m: Optional[float] = None
    main_engines: Optional[str] = None
    bollard_pull_t: Optional[float] = None
    design_speed_kn: Optional[float] = None
    import_file_id: Optional[int] = None
    extras: Optional[Dict[str, Any]] = None


class PortCraftListResponse(BaseModel):
    items: List[PortCraftOut]
    total: int
    limit: int
    offset: int
    count: int


class TypeStat(BaseModel):
    craft_type: Optional[str] = None
    count: int


class OwnershipStat(BaseModel):
    owned_or_hired: Optional[str] = None
    count: int


class PortCraftStatsOut(BaseModel):
    total: int
    by_type: List[TypeStat]
    by_ownership: List[OwnershipStat]


def _filters(craft_type, owned_or_hired, name, owner) -> Dict[str, Any]:
    return {"craft_type": craft_type, "owned_or_hired": owned_or_hired,
            "name": name, "owner": owner}


@router.get("", response_model=PortCraftListResponse,
            summary="List / filter the UC-I port-craft fleet register")
async def list_craft(
    craft_type: Optional[str] = Query(default=None),
    owned_or_hired: Optional[str] = Query(default=None),
    name: Optional[str] = Query(default=None, description="name contains"),
    owner: Optional[str] = Query(default=None, description="owner contains"),
    sort: str = Query(default="name"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PortCraftService = Depends(get_service),
) -> PortCraftListResponse:
    res = await service.list_craft(_filters(craft_type, owned_or_hired, name, owner),
                                   sort=sort, direction=direction, limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return PortCraftListResponse(**res)


@router.get("/stats", response_model=PortCraftStatsOut,
            summary="Port-craft counts by type + ownership")
async def stats(service: PortCraftService = Depends(get_service)) -> PortCraftStatsOut:
    res = await service.stats(_filters(None, None, None, None))
    REQUESTS.labels(_API, "ok").inc()
    return PortCraftStatsOut(**res)


@router.get("/{craft_id}", response_model=PortCraftOut, summary="One port craft")
async def get_craft(craft_id: int,
                    service: PortCraftService = Depends(get_service)) -> PortCraftOut:
    res = await service.get(craft_id)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "port_craft_not_found", "craft_id": craft_id})
    REQUESTS.labels(_API, "ok").inc()
    return PortCraftOut(**res)
