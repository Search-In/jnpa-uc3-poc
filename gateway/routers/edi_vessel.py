"""/api/edi — COARRI/COPRAR vessel-side container moves (read-only, additive).

Read path for the 0125 tables the jnpa_sync router fills from the
edi-messages group (services.edi_vessel consumer). Same mould as
gateway/routers/rail.py: thin over the repository, LIVE/DEMO narrowed via the
shared ``data_mode`` dependency.

    GET /api/edi/summary                  -> per-doc-type/direction KPIs
    GET /api/edi/vessel-moves             -> container rows (filter/search/page)
    GET /api/edi/container/{container_no} -> vessel leg for follow-the-box
    GET /api/edi/uploads                  -> import ledger

RBAC: /api/edi is not in gateway/auth.py._POLICY, so it inherits the default
"any authenticated role" rule (read-only). No auth change is required or made.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from ..data_mode import data_mode
from services.edi_vessel.repository import EdiVesselRepository

router = APIRouter(prefix="/api/edi", tags=["edi"])

_repo: Optional[EdiVesselRepository] = None


def get_repo(request: Request) -> EdiVesselRepository:
    global _repo
    if _repo is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _repo = EdiVesselRepository(getattr(cfg, "postgres_dsn", None) or None)
    return _repo


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _page(items: List[dict], total: int, limit: int, offset: int,
          response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset,
                count=len(items))


@router.get("/summary")
async def edi_summary(
    repo: EdiVesselRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Dict[str, Any]:
    """Containers / vessel calls per doc type and direction (COARRI report
    vs COPRAR advance order, LOAD vs DISCHARGE)."""
    return await repo.summary(data_origin=origin)


@router.get("/vessel-moves", response_model=Page)
async def edi_vessel_moves(
    response: Response,
    q: Optional[str] = Query(None, description="container / VCN / line / doc search"),
    doc_type: Optional[str] = Query(None, description="COARRI | COPRAR"),
    direction: Optional[str] = Query(None, description="LOAD | DISCHARGE"),
    vcn: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repo: EdiVesselRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Page:
    items, total = await repo.list_moves(
        data_origin=origin, doc_type=doc_type, direction=direction,
        vcn=vcn, q=q, limit=limit, offset=offset)
    return _page(items, total, limit, offset, response)


@router.get("/container/{container_no}")
async def edi_container(
    container_no: str,
    repo: EdiVesselRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Dict[str, Any]:
    """COARRI/COPRAR rows for one container — the ship↔yard leg the
    follow-the-box / UC-3 lifecycle timeline can splice in."""
    return await repo.container_view(container_no, data_origin=origin)


@router.get("/uploads", response_model=Page)
async def edi_uploads(
    response: Response,
    feed: Optional[str] = Query(None, description="COARRI | COPRAR"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: EdiVesselRepository = Depends(get_repo),
) -> Page:
    items, total = await repo.list_uploads(feed=feed, limit=limit,
                                           offset=offset)
    return _page(items, total, limit, offset, response)
