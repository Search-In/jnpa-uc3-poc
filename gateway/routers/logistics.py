"""/api/logistics — ULIP Logistics Intelligence for the JNPA corridor.

ADDITIVE — same mould as gateway/routers/traffic.py's TomTom endpoints: a
thin router over :class:`services.logistics.LogisticsService` →
integrations/ulip, credential-gated via ULIP_CLIENT_ID + ULIP_CLIENT_SECRET
(or a pre-issued ULIP_API_KEY), backend-only, never exposed to the browser.
A ULIP outage NEVER breaks these surfaces —
LIVE → CACHED (Redis) → DATABASE (core.logistics_*) → FALLBACK (explicitly
empty, clearly tagged — no fabricated shipment data):

    GET /api/logistics/health         -> ULIP integration posture
    GET /api/logistics/current        -> corridor logistics summary
    GET /api/logistics/tracking/{id}  -> vehicle / container tracking
    GET /api/logistics/events         -> persisted event history (paged)

RBAC: /api/logistics has no dedicated _POLICY entry, so — like /api/traffic
and /api/air-quality — it is visible to any authenticated stakeholder when
AUTH_ENABLED=true (see gateway/auth.py).

Distinct, untouched neighbours: /api/ulip (trucking-app GPS relay proxy),
/api/fastag (FASTag vertical), /api/ldb (LDB adapter over the generic
integration seam).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request

from ..logging import get_logger
from ..metrics import REQUESTS

from services.logistics import LogisticsService

log = get_logger("gateway.logistics")

router = APIRouter(prefix="/api/logistics", tags=["logistics"])

_service: Optional[LogisticsService] = None


def get_service(request: Request) -> LogisticsService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        from integrations.ulip import UlipClient

        try:
            cache_ttl = int(os.environ.get("GATEWAY_CACHE_TTL_ULIP_S", "300"))
        except ValueError:
            cache_ttl = 300
        _service = LogisticsService(
            dsn=getattr(cfg, "postgres_dsn", None) or None,
            # Credentials empty -> LIVE rung disabled; the surfaces still
            # answer from the CACHED/DATABASE rungs (or the clearly-tagged
            # empty FALLBACK) and say so in the metadata.
            client=UlipClient(
                api_url=getattr(cfg, "ulip_api_url", "") or None,
                api_key=getattr(cfg, "ulip_api_key", None),
                client_id=getattr(cfg, "ulip_client_id", None),
                client_secret=getattr(cfg, "ulip_client_secret", None),
            ),
            cache_ttl_s=cache_ttl,
        )
    return _service


# ------------------------------------------------------------------- health
@router.get("/health", summary="ULIP logistics integration posture")
async def logistics_health(svc: LogisticsService = Depends(get_service)) -> Dict[str, Any]:
    return await svc.health()


# ------------------------------------------------------------------ current
@router.get("/current",
            summary="Corridor logistics summary (events, tracked references)")
async def current_logistics(svc: LogisticsService = Depends(get_service)) -> Dict[str, Any]:
    result = await svc.current()
    REQUESTS.labels("logistics", "ok" if result["status"] == "LIVE" else "error").inc()
    return result


# ----------------------------------------------------------------- tracking
@router.get("/tracking/{ref_id}",
            summary="ULIP tracking for one vehicle registration or "
                    "ISO-6346 container number")
async def logistics_tracking(
    ref_id: str = PathParam(..., min_length=4, max_length=20,
                            description="Vehicle registration number "
                                        "(e.g. MH46AB1234) or ISO-6346 "
                                        "container number (e.g. MSKU1234565)"),
    svc: LogisticsService = Depends(get_service),
) -> Dict[str, Any]:
    ref = ref_id.strip().upper()
    if not ref.isalnum():
        raise HTTPException(status_code=400,
                            detail={"error": "invalid_ref",
                                    "hint": "alphanumeric registration or "
                                            "container number expected"})
    result = await svc.tracking(ref)
    REQUESTS.labels("logistics", "ok" if result["status"] == "LIVE" else "error").inc()
    return result


# ------------------------------------------------------------------- events
@router.get("/events", summary="Persisted logistics event history (paged)")
async def logistics_events(
    ref_id: Optional[str] = Query(None, min_length=4, max_length=20),
    event_type: Optional[str] = Query(None, max_length=40),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    svc: LogisticsService = Depends(get_service),
) -> Dict[str, Any]:
    try:
        items, total = await svc.events(ref_id=ref_id, event_type=event_type,
                                        limit=limit, offset=offset)
    except Exception as exc:  # noqa: BLE001 - DB down => empty page, not a 500
        log.warning("logistics_events_failed", error=str(exc))
        items, total = [], 0
    REQUESTS.labels("logistics", "ok").inc()
    return {"events": items, "count": len(items), "total": total,
            "limit": limit, "offset": offset}
