"""/api/marine/sea-channels — UC-I Marine sea-channel geometry (read-only, additive).

A thin router over :class:`services.marine.sea_channel.SeaChannelService`, in the same
mould as gateway/routers/marine_port_craft.py. Serves the JNPA channel polygons ingested
through the SHARED marine upload endpoints (JNPA_Sea_Channels shapefile ZIP) — there is
NO separate sea-channel upload endpoint.

    GET /api/marine/sea-channels          -> list + filter (includes geom_geojson)
    GET /api/marine/sea-channels/geojson  -> a WGS84 FeatureCollection for the map
    GET /api/marine/sea-channels/stats    -> counts + total area by channel name
    GET /api/marine/sea-channels/{id}     -> one channel

Reads ONLY core.sea_channel. RBAC: covered by the existing ("/api/marine", …) policy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.sea_channel import SeaChannelService

router = APIRouter(prefix="/api/marine/sea-channels", tags=["marine"])

_API = "marine_sea_channel"
_service: Optional[SeaChannelService] = None


def get_service(request: Request) -> SeaChannelService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = SeaChannelService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class SeaChannelOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    channel_id: Optional[int] = None
    name: Optional[str] = None
    section_label: Optional[str] = None
    area_ha: Optional[float] = None
    length_m: Optional[float] = None
    geom_geojson: Optional[Dict[str, Any]] = None
    import_file_id: Optional[int] = None


class SeaChannelListResponse(BaseModel):
    items: List[SeaChannelOut]
    total: int
    limit: int
    offset: int
    count: int


class NameStat(BaseModel):
    name: Optional[str] = None
    count: int
    area_ha: Optional[float] = None


class SeaChannelStatsOut(BaseModel):
    total: int
    by_name: List[NameStat]


def _filters(name, section) -> Dict[str, Any]:
    return {"name": name, "section": section}


@router.get("", response_model=SeaChannelListResponse,
            summary="List / filter UC-I sea channels (with GeoJSON geometry)")
async def list_channels(
    name: Optional[str] = Query(default=None, description="name contains"),
    section: Optional[str] = Query(default=None, description="section label contains"),
    sort: str = Query(default="name"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: SeaChannelService = Depends(get_service),
) -> SeaChannelListResponse:
    res = await service.list_channels(_filters(name, section), sort=sort, direction=direction,
                                      limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return SeaChannelListResponse(**res)


@router.get("/geojson", summary="Sea channels as a WGS84 GeoJSON FeatureCollection")
async def geojson(
    name: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1000),
    service: SeaChannelService = Depends(get_service),
) -> Dict[str, Any]:
    res = await service.geojson(_filters(name, None), limit=limit)
    REQUESTS.labels(_API, "ok").inc()
    return res


@router.get("/stats", response_model=SeaChannelStatsOut,
            summary="Sea-channel counts + total area by name")
async def stats(service: SeaChannelService = Depends(get_service)) -> SeaChannelStatsOut:
    res = await service.stats(_filters(None, None))
    REQUESTS.labels(_API, "ok").inc()
    return SeaChannelStatsOut(**res)


@router.get("/{channel_id}", response_model=SeaChannelOut, summary="One sea channel")
async def get_channel(channel_id: int,
                      service: SeaChannelService = Depends(get_service)) -> SeaChannelOut:
    res = await service.get(channel_id)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "sea_channel_not_found", "channel_id": channel_id})
    REQUESTS.labels(_API, "ok").inc()
    return SeaChannelOut(**res)
