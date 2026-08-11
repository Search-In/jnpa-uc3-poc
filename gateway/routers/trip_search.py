"""/api/trip — universal trip resolver (UC3-024) + visit timeline (UC3-025).

    GET /api/trip/search?q=      -> resolve a plate / container / e-seal / Form 13
                                    number to the SAME trip, with match confidence
    GET /api/trip/{trip_id}      -> that trip: identity, documents, timeline
    GET /api/trip/keys/supported -> the key kinds the box accepts (UI hinting)

Resolution never guesses. A key matching several visits returns them all with
``status="AMBIGUOUS"`` and ``resolved_trip_id: null``; a key matching nothing
returns ``status="NO_MATCH"`` with near-miss suggestions.

RBAC: read-only, so it inherits the default "any authenticated role" rule — the
same audience as /api/gate-documents, whose rows it reads.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..metrics import REQUESTS
from services.trip_search import TripSearchService

router = APIRouter(prefix="/api/trip", tags=["trip-search"])

_service: Optional[TripSearchService] = None


def get_service(request: Request) -> TripSearchService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = TripSearchService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("/search")
async def search(
    q: str = Query(..., description="plate | container | e-seal | Form 13 no | PIN"),
    svc: TripSearchService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("trip_search", "ok").inc()
    return await svc.resolve(q)


@router.get("/keys/supported")
async def supported_keys(svc: TripSearchService = Depends(get_service)) -> Dict[str, Any]:
    out = await svc.resolve("")
    REQUESTS.labels("trip_search", "ok").inc()
    return {"keys": out["searchable_keys"]}


@router.get("/{trip_id}")
async def trip(trip_id: str,
               svc: TripSearchService = Depends(get_service)) -> Dict[str, Any]:
    row = await svc.trip(trip_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "trip_not_found", "trip_id": trip_id})
    REQUESTS.labels("trip_search", "ok").inc()
    return row
