"""/api/auto-leo — the Auto-LEO reconciliation board (UC3-040).

    GET /api/auto-leo/board                  -> one row per export container
    GET /api/auto-leo/container/{container}   -> one container's four-way join
    GET /api/auto-leo/flags                   -> the flag legend

Each row reports the four evidence streams (e-seal, Form 13, weighbridge,
ICEGATE) as MATCH / MISMATCH / MISSING, and ``leo_ready`` is true only when all
four pass. Weighbridge and ICEGATE rows are labelled SIMULATED: those feeds do
not exist in the supplied corpus (gaps G8/G10) and are simulated around the REAL
Form 13 values rather than presented as captured.

RBAC: read-only, so it inherits the default "any authenticated role" rule — the
same audience as /api/gate-data, whose captures it joins.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..metrics import REQUESTS
from services.auto_leo import AutoLeoService

router = APIRouter(prefix="/api/auto-leo", tags=["auto-leo"])

_service: Optional[AutoLeoService] = None


def get_service(request: Request) -> AutoLeoService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = AutoLeoService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("/board")
async def board(
    limit: int = Query(50, ge=1, le=500),
    source_mode: Optional[str] = Query(None, description="live | sim"),
    svc: AutoLeoService = Depends(get_service),
) -> Dict[str, Any]:
    REQUESTS.labels("auto_leo", "ok").inc()
    return await svc.board(limit=limit, source_mode=source_mode)


@router.get("/container/{container_no}")
async def container(container_no: str,
                    svc: AutoLeoService = Depends(get_service)) -> Dict[str, Any]:
    row = await svc.container(container_no.strip().upper())
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_found",
                                    "container_no": container_no})
    REQUESTS.labels("auto_leo", "ok").inc()
    return row


@router.get("/flags")
async def flags(svc: AutoLeoService = Depends(get_service)) -> Dict[str, Any]:
    out = await svc.board(limit=1)
    REQUESTS.labels("auto_leo", "ok").inc()
    return {"flags": out["flags"], "weight_tolerance_pct": out["weight_tolerance_pct"],
            "assumption": out["assumption"]}
