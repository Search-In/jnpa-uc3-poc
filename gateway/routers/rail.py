"""/api/rail — Rail feeds landed by the JNPA Port-Data sync (read-only, additive).

Until now the ``rail-fois`` / ``rail-form11-icd`` groups were parsed into the
0119 tables (``core.fois_train_intimation`` / ``core.form11_entry`` /
``core.cto_manifest_entry`` + the ``core.rail_import_file`` ledger) but no
router served them — landed data with no read path. This router closes that
gap in the same mould as gateway/routers/cfs_ecy.py: a thin layer over
:class:`services.rail.RailRepository`, LIVE/DEMO narrowed via the shared
``data_mode`` dependency (0121 tagged the rail tables for exactly this).

    GET /api/rail/summary                  -> per-feed KPI card
    GET /api/rail/fois                     -> rake pre-advice / train intimations
    GET /api/rail/form11                   -> Form 11 rake-placement entries
    GET /api/rail/cto                      -> CTO wagon-manifest entries
    GET /api/rail/container/{container_no} -> rail leg for follow-the-box
    GET /api/rail/uploads                  -> import ledger (dump + API sync)

RBAC: /api/rail is not in gateway/auth.py._POLICY, so it inherits the default
"any authenticated role" rule (read-only). No auth change is required or made.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from ..data_mode import data_mode
from services.rail.repository import RailRepository

router = APIRouter(prefix="/api/rail", tags=["rail"])

_repo: Optional[RailRepository] = None


def get_repo(request: Request) -> RailRepository:
    global _repo
    if _repo is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _repo = RailRepository(getattr(cfg, "postgres_dsn", None) or None)
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
async def rail_summary(
    repo: RailRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Dict[str, Any]:
    """Per-feed KPIs: FOIS rakes (total / inbound), Form 11 containers, CTO
    wagons — the rail card for the UC-III congestion picture."""
    return await repo.summary(data_origin=origin)


@router.get("/fois", response_model=Page)
async def rail_fois(
    response: Response,
    q: Optional[str] = Query(None, description="rake / station search"),
    loaded_empty: Optional[str] = Query(None, description="L | E flag"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repo: RailRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Page:
    items, total = await repo.list_fois(
        data_origin=origin, loaded_empty=loaded_empty, q=q,
        limit=limit, offset=offset)
    return _page(items, total, limit, offset, response)


@router.get("/form11", response_model=Page)
async def rail_form11(
    response: Response,
    q: Optional[str] = Query(None, description="container / booking / ICD search"),
    terminal: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repo: RailRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Page:
    items, total = await repo.list_form11(
        data_origin=origin, terminal=terminal, q=q,
        limit=limit, offset=offset)
    return _page(items, total, limit, offset, response)


@router.get("/cto", response_model=Page)
async def rail_cto(
    response: Response,
    q: Optional[str] = Query(None, description="container / wagon / rake search"),
    cto_code: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    repo: RailRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Page:
    items, total = await repo.list_cto(
        data_origin=origin, cto_code=cto_code, q=q,
        limit=limit, offset=offset)
    return _page(items, total, limit, offset, response)


@router.get("/container/{container_no}")
async def rail_container(
    container_no: str,
    repo: RailRepository = Depends(get_repo),
    origin: Optional[str] = Depends(data_mode),
) -> Dict[str, Any]:
    """Form 11 + CTO rows for one container — the rail leg that the
    follow-the-box / UC-3 lifecycle timeline can splice in."""
    return await repo.container_rail_view(container_no, data_origin=origin)


@router.get("/uploads", response_model=Page)
async def rail_uploads(
    response: Response,
    feed: Optional[str] = Query(None, description="FOIS | FORM11 | CTO"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo: RailRepository = Depends(get_repo),
) -> Page:
    items, total = await repo.list_uploads(feed=feed, limit=limit,
                                           offset=offset)
    return _page(items, total, limit, offset, response)
