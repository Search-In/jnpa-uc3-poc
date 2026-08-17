"""/api/dq — the Data Quality ledger (read-only, additive).

``core.dq_issue`` is where every corpus importer in this repository records what
it found wrong with the customer's source data and chose to keep rather than
repair. Until now nothing served it, so the findings were invisible outside the
importer's console. This router exposes the existing ledger; it defines no new
storage and writes nothing.

    GET /api/dq/issues   -> list / filter / paginate findings
    GET /api/dq/summary  -> roll-up by severity, source table and issue type

Filters mirror the table's own columns: ``source_table``, ``issue_type``,
``severity`` (info|warn|error), ``file_id`` and a free-text ``q`` over the
description and record reference.

RBAC: /api/dq is not in gateway/auth.py._POLICY, so it inherits the default "any
authenticated role" rule (read-only), exactly like /api/cfs-ecy. No auth change
is required or made.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from ..datewindow import DateWindow, date_window
from ..metrics import REQUESTS
from services.dq import DqService
from services.dq.repository import VALID_SEVERITIES

router = APIRouter(prefix="/api/dq", tags=["data-quality"])

_service: Optional[DqService] = None


def get_service(request: Request) -> DqService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = DqService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class IssuePage(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _severity(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().lower()
    if v not in VALID_SEVERITIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_severity", "severity": value,
                                    "allowed": list(VALID_SEVERITIES)})
    return v


def _filters(source_table, issue_type, severity, file_id, q) -> Dict[str, Any]:
    return {"source_table": source_table, "issue_type": issue_type,
            "severity": _severity(severity), "file_id": file_id, "q": q}


@router.get("/issues", response_model=IssuePage,
            summary="Data-quality findings recorded by the corpus importers")
async def list_issues(
    response: Response,
    source_table: Optional[str] = Query(default=None,
                                        description="e.g. core.container_event"),
    issue_type: Optional[str] = Query(default=None,
                                      description="e.g. count_mismatch"),
    severity: Optional[str] = Query(default=None, description="info | warn | error"),
    file_id: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None, description="free text in description/ref"),
    sort: str = Query(default="severity"),
    order: str = Query(default="asc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    svc: DqService = Depends(get_service),
    window: DateWindow = Depends(date_window),
) -> IssuePage:
    # GAP-DATE-01: the window rides with the filters into the shared
    # where-builder; the column is stated here, never inferred there.
    _f = _filters(source_table, issue_type, severity, file_id, q)
    _f["_window"] = window
    _f["_date_col"] = "detected_at"
    res = await svc.list_issues(_f,
                                sort=sort, direction=order, limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(res["total"])
    REQUESTS.labels("dq", "ok").inc()
    return IssuePage(**res)


@router.get("/summary", summary="Data-quality roll-up by severity / source / type")
async def summary(
    source_table: Optional[str] = Query(default=None),
    issue_type: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    file_id: Optional[int] = Query(default=None),
    q: Optional[str] = Query(default=None),
    svc: DqService = Depends(get_service),
) -> Dict[str, Any]:
    res = await svc.summary(_filters(source_table, issue_type, severity, file_id, q))
    REQUESTS.labels("dq", "ok").inc()
    return res
