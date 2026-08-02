"""/api/marine — UC-I Marine Data-Upload sub-module (CSV, additive).

A thin router over :class:`services.marine.MarineUploadService`, in the same mould as
the upload section of gateway/routers/cfs_ecy.py. It serves the reusable upload workflow
for the vessel-call spine: template → validate (dry-run preview) → confirm import →
history. Writes ONLY core.marine_import_* + core.vessel_call (via the repository);
touches NOTHING in jnpa.

    GET  /api/marine/templates/vessel-call -> download the CSV template
    POST /api/marine/validate              -> dry-run parse + preview (no DB write)
    POST /api/marine/upload                -> import (sha256 dedup + VCN upsert; idempotent)
    GET  /api/marine/uploads               -> upload history (import ledger)
    GET  /api/marine/uploads/{file_id}     -> one upload + its row errors

SCOPE (this release): CSV only. The service rejects a non-CSV file.

RBAC: /api/marine is gated to CONTROL_ROOM + CUSTOMS (+ admin ⊂ control room) in
gateway/auth.py — the same policy entry that covers /api/marine/calls. The write path is
additionally guarded per-request by require_uploader().
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel

from ..auth import CONTROL_ROOM, Role, auth_enabled
from ..metrics import REQUESTS
from services.marine import MarineUploadService
from services.marine.parsers import DocumentTypeMismatch, UnknownDocumentType

router = APIRouter(prefix="/api/marine", tags=["marine"])

_API = "marine_imports"
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the other upload modules
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

_upload_service: Optional[MarineUploadService] = None


def get_upload_service(request: Request) -> MarineUploadService:
    global _upload_service
    if _upload_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _upload_service = MarineUploadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
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
                                    "detail": "Marine upload requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "empty_file"})
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={"error": "file_too_large", "max_bytes": _MAX_UPLOAD_BYTES})
    return content


def _document_type_error(exc: Exception) -> HTTPException:
    """Map a client-supplied document_type fault onto HTTP 400.

    Same ``{"error": ..., "detail": ...}`` envelope the existing transport guards use
    (empty_file / file_too_large). Unreachable for a client that omits document_type, so
    no existing caller can start seeing a 400 it did not see before."""
    if isinstance(exc, UnknownDocumentType):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                             detail={"error": "unknown_document_type",
                                     "detail": str(exc),
                                     "document_type": exc.raw,
                                     "accepted": list(exc.accepted)})
    assert isinstance(exc, DocumentTypeMismatch)
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                         detail={"error": "document_type_mismatch",
                                 "detail": str(exc),
                                 "document_type": exc.declared,
                                 "detected_format": exc.detected,
                                 "expected_formats": list(exc.expected)})


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _page(items: List[dict], total: int, limit: int, offset: int, response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


# --------------------------------------------------------------------- endpoints
@router.get("/templates/vessel-call", summary="Download the Marine vessel-call CSV template")
async def upload_template(request: Request,
                          svc: MarineUploadService = Depends(get_upload_service)) -> Response:
    require_uploader(request)
    csv_text = svc.template()
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="marine_vessel_call_template.csv"'})


@router.post("/validate", summary="Validate a Marine CSV upload (dry-run: parse + preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          document_type: Optional[str] = Form(default=None),
                          svc: MarineUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    content = await _read_upload(file)
    try:
        res = await svc.validate(content, file.filename or "upload.csv", uploader,
                                 document_type=document_type)
    except (UnknownDocumentType, DocumentTypeMismatch) as exc:
        REQUESTS.labels(_API, "error").inc()
        raise _document_type_error(exc) from exc
    REQUESTS.labels(_API, "ok").inc()
    return res


@router.post("/upload", summary="Import a Marine CSV upload (valid rows upserted; idempotent)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        document_type: Optional[str] = Form(default=None),
                        svc: MarineUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    content = await _read_upload(file)
    try:
        res = await svc.import_file(content, file.filename or "upload.csv", uploader,
                                    document_type=document_type)
    except (UnknownDocumentType, DocumentTypeMismatch) as exc:
        REQUESTS.labels(_API, "error").inc()
        raise _document_type_error(exc) from exc
    REQUESTS.labels(_API, "ok").inc()
    return res


@router.get("/uploads", response_model=Page, summary="Marine upload history (import ledger)")
async def upload_history(
    response: Response,
    request: Request,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default=None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: MarineUploadService = Depends(get_upload_service),
) -> Page:
    require_uploader(request)
    filters = {"status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One Marine upload with its row errors")
async def upload_detail(file_id: int, request: Request,
                        svc: MarineUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res
