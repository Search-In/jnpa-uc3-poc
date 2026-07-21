"""/api/berthing — Berthing Reports (UC-III module 7, additive).

A thin router over :class:`services.berthing.BerthingService` (reads) and
:class:`BerthingUploadService` (the reusable Data-Upload sub-module), in the same
mould as gateway/routers/cfs_ecy.py. It serves the normalised vessel-call model for
the five JNPA container terminals (APMT / BMCT / NSFT / NSICT / NSIGT), a per-call
lifecycle timeline, KPI aggregates, and the template → validate → import → history
upload workflow. It writes ONLY jnpa.berthing_* — cargo / shipping_lines / cfs_ecy are
untouched (soft value-links only).

    GET  /api/berthing                    -> list + filter/search/paginate vessel calls
    GET  /api/berthing/stats              -> KPI aggregates + per-terminal counts
    GET  /api/berthing/{id}               -> one vessel call
    GET  /api/berthing/{id}/timeline      -> one call + its lifecycle events
    GET  /api/berthing/templates/{terminal} -> download an upload template
    POST /api/berthing/validate           -> dry-run parse + preview (no import)
    POST /api/berthing/upload             -> import (idempotent upsert)
    GET  /api/berthing/uploads            -> upload history (import ledger)
    GET  /api/berthing/uploads/{id}       -> one upload + its row errors

RBAC: /api/berthing is gated to CONTROL_ROOM + CUSTOMS (+ admin ⊂ control room) in
gateway/auth.py, matching the other Data-Upload modules.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel, ConfigDict

from ..auth import CONTROL_ROOM, Role, auth_enabled
from ..metrics import REQUESTS
from services.berthing import BerthingService, BerthingUploadService
from services.berthing import upload_parsers as P

router = APIRouter(prefix="/api/berthing", tags=["berthing"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the other upload modules
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

_service: Optional[BerthingService] = None
_upload_service: Optional[BerthingUploadService] = None


def get_service(request: Request) -> BerthingService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = BerthingService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def get_upload_service(request: Request) -> BerthingUploadService:
    global _upload_service
    if _upload_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _upload_service = BerthingUploadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _upload_service


def require_uploader(request: Request) -> str:
    """Upload write-gate — reads the auth-middleware principal WITHOUT modifying auth /
    JWT / RBAC (mirrors cfs_ecy.require_uploader). Dev/mock (AUTH off) → 'dev'."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "upload_forbidden",
                                    "detail": "Berthing upload requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


def _terminal_selector(value: Optional[str]) -> Optional[str]:
    """Normalize the template/upload terminal selector. 'ALL'/'ANY'/blank → None
    (the per-row Terminal column is then required for each row)."""
    v = (value or "").strip()
    if not v or v.upper() in ("ALL", "ANY"):
        return None
    canon = P.terminal_ok(v)
    if canon is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_terminal", "terminal": value,
                                    "allowed": list(P.TERMINALS) + ["ALL"]})
    return canon


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "empty_file"})
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={"error": "file_too_large", "max_bytes": _MAX_UPLOAD_BYTES})
    return content


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _page(items: List[dict], total: int, limit: int, offset: int, response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


# --------------------------------------------------------------------- DTOs
class ReportOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Optional[int] = None
    terminal: Optional[str] = None
    vessel_name: Optional[str] = None
    imo_number: Optional[str] = None
    voyage_number: Optional[str] = None
    shipping_line: Optional[str] = None
    berth_number: Optional[str] = None
    eta: Optional[datetime] = None
    ata: Optional[datetime] = None
    berthing_time: Optional[datetime] = None
    departure_time: Optional[datetime] = None
    cargo_operation_start: Optional[datetime] = None
    cargo_operation_end: Optional[datetime] = None
    status: Optional[str] = None
    source_file: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReportListResponse(BaseModel):
    items: List[ReportOut]
    total: int
    limit: int
    offset: int
    count: int


class TerminalStat(BaseModel):
    terminal: str
    count: int
    berthed: int


class StatsOut(BaseModel):
    total: int
    expected: int
    arrived: int
    berthed: int
    completed: int
    departed: int
    terminals: int
    avg_berth_hours: Optional[float] = None
    by_terminal: List[TerminalStat]


# ------------------------------------------------------------------- helpers
def _status(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper().replace(" ", "_").replace("-", "_")
    if v not in P.STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_status", "status": value})
    return v


def _terminal_filter(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    canon = P.terminal_ok(value)
    if canon is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_terminal", "terminal": value})
    return canon


def _filters(terminal, status_, vessel, voyage, berthed_only, eta_from, eta_to) -> Dict[str, Any]:
    return {"terminal": _terminal_filter(terminal), "status": _status(status_),
            "vessel": vessel, "voyage": voyage,
            "berthed_only": bool(berthed_only), "eta_from": eta_from, "eta_to": eta_to}


# ------------------------------------------------------------------- read endpoints
@router.get("", response_model=ReportListResponse, summary="List / search berthing vessel calls")
async def list_reports(
    terminal: Optional[str] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status"),
    vessel: Optional[str] = Query(default=None, description="vessel name contains"),
    voyage: Optional[str] = Query(default=None, description="voyage / VIA contains"),
    berthed_only: bool = Query(default=False),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    sort: str = Query(default="updated_at"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: BerthingService = Depends(get_service),
) -> ReportListResponse:
    filters = _filters(terminal, status_, vessel, voyage, berthed_only, date_from, date_to)
    res = await service.list_reports(filters, sort=sort, direction=direction,
                                     limit=limit, offset=offset)
    REQUESTS.labels("berthing", "ok").inc()
    return ReportListResponse(**res)


@router.get("/stats", response_model=StatsOut, summary="Berthing KPI aggregates + per-terminal counts")
async def stats(
    terminal: Optional[str] = Query(default=None),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    service: BerthingService = Depends(get_service),
) -> StatsOut:
    filters = _filters(terminal, None, None, None, False, date_from, date_to)
    res = await service.stats(filters)
    REQUESTS.labels("berthing", "ok").inc()
    return StatsOut(**res)


# ============================================================ Data Upload sub-module
@router.get("/templates/{terminal}", summary="Download a berthing upload template")
async def upload_template(terminal: str, request: Request,
                          svc: BerthingUploadService = Depends(get_upload_service)) -> Response:
    require_uploader(request)
    _terminal_selector(terminal)                        # validate (raises 400 if unknown)
    csv_text = svc.template()
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="berthing_template.csv"'})


@router.post("/validate", summary="Validate a berthing upload (dry-run: parse + preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          terminal: Optional[str] = Form(default=None),
                          svc: BerthingUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    term = _terminal_selector(terminal)
    content = await _read_upload(file)
    return await svc.validate(term, content, file.filename or "upload.csv", uploader)


@router.post("/upload", summary="Import a berthing upload (idempotent upsert)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        terminal: Optional[str] = Form(default=None),
                        svc: BerthingUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    term = _terminal_selector(terminal)
    content = await _read_upload(file)
    return await svc.import_file(term, content, file.filename or "upload.csv", uploader)


@router.get("/uploads", response_model=Page, summary="Berthing upload history (import ledger)")
async def upload_history(
    response: Response,
    request: Request,
    terminal: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: BerthingUploadService = Depends(get_upload_service),
) -> Page:
    require_uploader(request)
    filters = {"terminal": (P.terminal_ok(terminal) if terminal else None),
               "status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One berthing upload with its row errors")
async def upload_detail(file_id: int, request: Request,
                        svc: BerthingUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res


# ------------------------------------------------------------------- one call (declared last so
# the static /stats, /templates, /validate, /upload, /uploads prefixes win)
@router.get("/{report_id}", response_model=ReportOut, summary="One berthing vessel call")
async def get_report(report_id: int, service: BerthingService = Depends(get_service)) -> ReportOut:
    res = await service.get(report_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "report_not_found", "id": report_id})
    REQUESTS.labels("berthing", "ok").inc()
    return ReportOut(**res)


@router.get("/{report_id}/timeline", summary="One vessel call + its lifecycle timeline")
async def get_timeline(report_id: int,
                       service: BerthingService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.timeline(report_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "report_not_found", "id": report_id})
    REQUESTS.labels("berthing", "ok").inc()
    return res
