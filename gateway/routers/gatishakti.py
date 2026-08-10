"""/api/gatishakti — GatiShakti reference data for the JNPA corridor.

ADDITIVE — the same mould as gateway/routers/logistics.py: a thin router over
:class:`services.gatishakti.GatiShaktiService` -> integrations/ulip,
credential-gated via ULIP_CLIENT_ID + ULIP_CLIENT_SECRET (or a pre-issued
ULIP_API_KEY), backend-only, never exposed to the browser. A ULIP outage NEVER
breaks these surfaces — LIVE -> CACHED (Redis) -> DATABASE (core.gs_*) ->
FALLBACK (explicitly empty, clearly tagged; no fabricated road or plaza data):

    GET  /api/gatishakti/health       -> integration posture + row counts
    GET  /api/gatishakti/toll-plazas  -> NHAI toll plazas by state
    GET  /api/gatishakti/roads        -> road segments by state and/or NH no.
    GET  /api/gatishakti/road-points  -> named road points (lat/lon) by state
    POST /api/gatishakti/refresh      -> pull + persist the reference set

Reference data is slow-moving master data, so the reads serve from the DATABASE
rung by design and ``refresh`` is what re-pulls from ULIP — no read-path fetch,
because re-fetching a whole state's road network per request would burn the
subscription's call budget for data that changes a few times a year.

RBAC: like /api/logistics and /api/traffic there is no dedicated _POLICY entry,
so this is visible to any authenticated stakeholder when AUTH_ENABLED=true.
``refresh`` mutates reference tables, so it is a POST and is rate-limited by
its own cost rather than by policy.

No frontend screen consumes these directly in this phase; the toll-plaza
registry is consumed server-side by /api/fastag/toll-enroute.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query, Request

from ..logging import get_logger
from ..metrics import REQUESTS

from services.gatishakti import STATE_MAHARASHTRA, GatiShaktiService

log = get_logger("gateway.gatishakti")

router = APIRouter(prefix="/api/gatishakti", tags=["gatishakti"])

_service: Optional[GatiShaktiService] = None


def get_service(request: Request) -> GatiShaktiService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        from integrations.ulip import UlipClient

        try:
            cache_ttl = int(os.environ.get("GATEWAY_CACHE_TTL_GATISHAKTI_S", "3600"))
        except ValueError:
            cache_ttl = 3600
        _service = GatiShaktiService(
            dsn=getattr(cfg, "postgres_dsn", None) or None,
            # Credentials empty -> the LIVE refresh is unavailable; the reads
            # still answer from DATABASE (or the tagged empty FALLBACK).
            client=UlipClient(
                api_url=getattr(cfg, "ulip_api_url", "") or None,
                api_key=getattr(cfg, "ulip_api_key", None),
                client_id=getattr(cfg, "ulip_client_id", None),
                client_secret=getattr(cfg, "ulip_client_secret", None),
            ),
            cache_ttl_s=cache_ttl,
        )
    return _service


@router.get("/health", summary="GatiShakti integration posture + row counts")
async def gatishakti_health(
    svc: GatiShaktiService = Depends(get_service),
) -> Dict[str, Any]:
    return await svc.health()


@router.get("/toll-plazas", summary="NHAI toll plazas for one state (GATISHAKTI/04)")
async def toll_plazas(
    state_id: str = Query(STATE_MAHARASHTRA, min_length=1, max_length=10,
                          description="LGD state code — 27 = Maharashtra"),
    limit: int = Query(500, ge=1, le=5000),
    svc: GatiShaktiService = Depends(get_service),
) -> Dict[str, Any]:
    result = await svc.toll_plazas(state_id=state_id, limit=limit)
    REQUESTS.labels("gatishakti", "ok" if result["data_available"] else "error").inc()
    return result


@router.get("/roads", summary="Road segments by state and/or NH number "
                              "(GATISHAKTI/02, /01)")
async def roads(
    state_id: Optional[str] = Query(None, max_length=10),
    nh_no: Optional[str] = Query(None, max_length=10,
                                 description="e.g. NH-348 (the JNPA corridor)"),
    limit: int = Query(500, ge=1, le=5000),
    svc: GatiShaktiService = Depends(get_service),
) -> Dict[str, Any]:
    result = await svc.roads(state_id=state_id, nh_no=nh_no, limit=limit)
    REQUESTS.labels("gatishakti", "ok" if result["data_available"] else "error").inc()
    return result


@router.get("/road-points", summary="Named road points with lat/lon "
                                    "(GATISHAKTI/03)")
async def road_points(
    state_id: str = Query(STATE_MAHARASHTRA, min_length=1, max_length=10),
    limit: int = Query(1000, ge=1, le=5000),
    svc: GatiShaktiService = Depends(get_service),
) -> Dict[str, Any]:
    result = await svc.road_points(state_id=state_id, limit=limit)
    REQUESTS.labels("gatishakti", "ok" if result["data_available"] else "error").inc()
    return result


@router.post("/refresh", summary="Pull the GatiShakti reference set from ULIP "
                                 "and persist it")
async def refresh(
    state_id: str = Body(STATE_MAHARASHTRA, embed=True),
    nh_no: Optional[str] = Body(None, embed=True),
    svc: GatiShaktiService = Depends(get_service),
) -> Dict[str, Any]:
    """Re-pull toll plazas, road points and the road network for one state.

    Per-API failures are reported rather than raised — a GatiShakti outage on
    one endpoint must not cost the others their refresh, and the caller needs
    to see exactly which of the four actually updated.
    """
    return await svc.refresh(state_id=state_id, nh_no=nh_no)


__all__ = ["router", "get_service"]
