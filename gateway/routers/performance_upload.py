"""/api/performance (upload) — Performance Data Upload Management (module 12 sub-module).

Admin-only WRITE surface that lets DTCCC_ADMIN users upload JNPA performance data
(CSV/XLSX) into the EXISTING jnpa.perf_* dashboard tables. Thin router over
services.performance.UploadService (UploadService → UploadRepository). Additive:
it creates only the upload lifecycle tables and inserts into perf_* via idempotent
ON CONFLICT. It does NOT modify auth/JWT/RBAC — admin enforcement reads the
principal the auth middleware already attaches (request.state.principal).

    GET  /api/performance/templates/{report_type}  -> download CSV template
    POST /api/performance/validate                 -> dry-run parse+validate+preview
    POST /api/performance/upload                    -> atomic import (all-or-nothing)
    GET  /api/performance/uploads                   -> upload history
    GET  /api/performance/uploads/{upload_id}       -> one upload + logs + errors

Same prefix as gateway/routers/performance.py; FastAPI merges the routes. The read
dashboard endpoints stay in that file untouched.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)

from ..auth import Role, auth_enabled
from ..metrics import REQUESTS
from services.performance import UploadService

router = APIRouter(prefix="/api/performance", tags=["performance-upload"])

_MAX_BYTES = 10 * 1024 * 1024          # 10 MB upload cap
_service: Optional[UploadService] = None


def get_service(request: Request) -> UploadService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = UploadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def require_admin(request: Request) -> str:
    """Admin-only gate. Reads the auth-middleware principal WITHOUT modifying auth /
    JWT / RBAC. When AUTH_ENABLED is off (dev/test), the whole app is open, so we
    allow and attribute to 'dev'. Returns the uploader identity for the audit."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    if principal is None or getattr(principal, "role", None) != Role.DTCCC_ADMIN.value:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "admin_required",
                                    "detail": "Performance data upload requires the DTCCC_ADMIN role"})
    return getattr(principal, "sub", "admin")


def _check_type(report_type: str) -> str:
    rt = (report_type or "").strip().lower()
    if rt not in ("daily_status", "monthly_teu", "ldb_report"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_report_type", "report_type": report_type})
    return rt


async def _read(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "empty_file"})
    if len(content) > _MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={"error": "file_too_large", "max_bytes": _MAX_BYTES})
    return content


# ------------------------------------------------------------------- endpoints
@router.get("/templates/{report_type}", summary="Download a CSV upload template")
async def template(report_type: str, request: Request,
                   service: UploadService = Depends(get_service)) -> Response:
    require_admin(request)
    rt = _check_type(report_type)
    csv_text = service.template(rt)
    REQUESTS.labels("performance_upload", "ok").inc()
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{rt}_template.csv"'})


@router.post("/validate", summary="Validate an upload (dry-run: parse + preview, no import)")
async def validate(request: Request,
                   file: UploadFile = File(...),
                   report_type: str = Form(...),
                   service: UploadService = Depends(get_service)) -> Dict[str, Any]:
    uploader = require_admin(request)
    rt = _check_type(report_type)
    content = await _read(file)
    res = await service.validate(rt, content, file.filename or "upload.csv", uploader)
    REQUESTS.labels("performance_upload", "ok").inc()
    return res


@router.post("/upload", summary="Import an upload (atomic — all-or-nothing rollback)")
async def upload(request: Request,
                 file: UploadFile = File(...),
                 report_type: str = Form(...),
                 service: UploadService = Depends(get_service)) -> Dict[str, Any]:
    uploader = require_admin(request)
    rt = _check_type(report_type)
    content = await _read(file)
    res = await service.import_file(rt, content, file.filename or "upload.csv", uploader)
    REQUESTS.labels("performance_upload", "ok").inc()
    return res


@router.get("/uploads", summary="Upload history")
async def uploads(request: Request,
                  report_type: Optional[str] = Query(default=None),
                  status_: Optional[str] = Query(default=None, alias="status"),
                  limit: int = Query(default=50, ge=1, le=200),
                  offset: int = Query(default=0, ge=0),
                  service: UploadService = Depends(get_service)) -> Dict[str, Any]:
    require_admin(request)
    filters = {"report_type": report_type, "status": status_}
    res = await service.list_uploads(filters, limit=limit, offset=offset)
    REQUESTS.labels("performance_upload", "ok").inc()
    return res


@router.get("/uploads/{upload_id}", summary="One upload with logs + validation errors")
async def upload_detail(upload_id: str, request: Request,
                        service: UploadService = Depends(get_service)) -> Dict[str, Any]:
    require_admin(request)
    res = await service.get_upload(upload_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "upload_id": upload_id})
    REQUESTS.labels("performance_upload", "ok").inc()
    return res
