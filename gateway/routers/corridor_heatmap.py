"""/api/corridor-heatmap — T-01 corridor congestion heatmap (UC3-020).

    GET /api/corridor-heatmap?offset_minutes=   -> 13 segments at one slider position

``offset_minutes`` is the time slider: negative into the past, 0 at now, positive
into the forecast. It is clamped to the -6 h … +2 h contract and the response
says whether it was clamped, so a caller cannot request a 12-hour forecast and be
handed one that looks as confident as a 15-minute one.

The DATA_MODE banner flips EXACTLY at now: at or before it the segments read
OBSERVED; strictly after it they read DERIVED and carry a confidence band that
widens with the horizon.

RBAC: read-only, so it inherits the default "any authenticated role" rule — the
same audience as /api/traffic and /api/corridor, whose geometry it shares.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from ..metrics import REQUESTS
from services.corridor_heatmap import CorridorHeatmapService

router = APIRouter(prefix="/api/corridor-heatmap", tags=["corridor-heatmap"])

_service: Optional[CorridorHeatmapService] = None


def get_service(request: Request) -> CorridorHeatmapService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CorridorHeatmapService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("")
@router.get("/")
async def heatmap(
    offset_minutes: int = Query(0, description="-360 (‑6 h) … +120 (+2 h); 0 = now"),
    svc: CorridorHeatmapService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("corridor_heatmap", "ok").inc()
    return await svc.heatmap(offset_minutes=offset_minutes)
