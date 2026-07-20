"""/api/td-upload — Transporters & Drivers reusable Data Upload sub-module (UC-III).

The template -> validate -> preview -> confirm-import -> history workflow for the
Transporter and Driver masters, mirroring gateway/routers/cfs_ecy.py's Data-Upload
endpoints exactly. A single combined module with an ENTITY selector (TRANSPORTER /
DRIVER) — the analogue of CFS-ECY's facility (CFS / ECY) — so one import ledger
(jnpa.td_import_files) carries both.

It REUSES the existing masters end to end (no duplicate business tables):
  * TRANSPORTER -> jnpa.transporters      (upsert on source_company_id)
  * DRIVER      -> jnpa.driver_master      (upsert on licence_no_norm)
Re-uploading identical bytes is a no-op (sha256 dedup); invalid rows are skipped with
friendly errors; existing rows are updated (upsert), never blindly duplicated.

    GET  /api/td-upload/templates/{entity}   -> download a TRANSPORTER/DRIVER template
    POST /api/td-upload/validate             -> dry-run: parse + preview, no DB write
    POST /api/td-upload/upload               -> confirm import (idempotent, audited)
    GET  /api/td-upload/uploads              -> import history (ledger, paginated)
    GET  /api/td-upload/uploads/{file_id}    -> one upload + its row errors

RBAC: /api/td-upload is gated in gateway/auth.py._POLICY to CONTROL_ROOM + CUSTOMS
(+ admin ⊂ control room), the same audience as /api/shipping-lines and /api/cfs-ecy.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel

from ..auth import CONTROL_ROOM, Role, auth_enabled
from ..metrics import REQUESTS
from services.transporters_drivers import TransportersDriversUploadService

router = APIRouter(prefix="/api/td-upload", tags=["transporters-drivers-upload"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the shipping-lines / cfs-ecy cap
# Roles allowed to upload: control room + customs (+ admin ⊂ control room).
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}
_ENTITIES = ("TRANSPORTER", "DRIVER")

_upload_service: Optional[TransportersDriversUploadService] = None


def get_upload_service(request: Request) -> TransportersDriversUploadService:
    global _upload_service
    if _upload_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _upload_service = TransportersDriversUploadService(
            dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _upload_service


def require_uploader(request: Request) -> str:
    """Upload write-gate. Reads the auth-middleware principal WITHOUT modifying auth /
    JWT / RBAC (mirrors cfs_ecy.require_uploader). Returns the uploader id for the
    audit. When AUTH_ENABLED is off (dev/mock), the app is open -> 'dev'."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "upload_forbidden",
                                    "detail": "Transporter/Driver upload requires "
                                              "CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


def _check_entity(entity: str) -> str:
    v = (entity or "").strip().upper()
    if v not in _ENTITIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_entity", "entity": entity,
                                    "allowed": list(_ENTITIES)})
    return v


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "empty_file"})
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


# ------------------------------------------------------------------- endpoints
@router.get("/templates/{entity}", summary="Download a Transporter/Driver upload template")
async def upload_template(entity: str, request: Request,
                          svc: TransportersDriversUploadService = Depends(get_upload_service)) -> Response:
    require_uploader(request)
    ent = _check_entity(entity)
    csv_text = svc.template(ent)
    fname = f"{ent.lower()}_upload_template.csv"
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/validate", summary="Validate an upload (dry-run: parse + preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          entity: str = Form(...),
                          svc: TransportersDriversUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    ent = _check_entity(entity)
    content = await _read_upload(file)
    REQUESTS.labels("td_upload", "ok").inc()
    return await svc.validate(ent, content, file.filename or "upload.csv", uploader)


@router.post("/upload", summary="Import an upload (valid rows upserted; idempotent)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        entity: str = Form(...),
                        svc: TransportersDriversUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    ent = _check_entity(entity)
    content = await _read_upload(file)
    REQUESTS.labels("td_upload", "ok").inc()
    return await svc.import_file(ent, content, file.filename or "upload.csv", uploader)


@router.get("/uploads", response_model=Page, summary="Transporter/Driver upload history (ledger)")
async def upload_history(
    response: Response,
    request: Request,
    entity: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default="UPLOAD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: TransportersDriversUploadService = Depends(get_upload_service),
) -> Page:
    require_uploader(request)
    filters = {"entity_type": (entity.strip().upper() if entity else None),
               "import_status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One upload with its row errors")
async def upload_detail(file_id: int, request: Request,
                        svc: TransportersDriversUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res
