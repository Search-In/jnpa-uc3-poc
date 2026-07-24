"""/api/shipping-lines — Shipping Lines document layer (module 4: IAL/EAL/EDO).

A thin router over :class:`services.shipping_lines.ShippingLinesService` (service →
raw-SQL ShippingLinesRepository), in the same mould as gateway/routers/customs.py.
It exposes the Import/Export Advance Lists and Electronic Delivery Orders imported
from the OFFICIAL JNPA customer files (migration 0032) plus an admin import trigger,
and cross-links a container to its shipping-line facts via the
mart.v_shipping_line_container view — a soft, by-value join to core.cargo. It
touches no existing table.

    GET  /api/shipping-lines/summary                     -> dashboard counts
    GET  /api/shipping-lines                             -> advance-list line items (filter+page)
    GET  /api/shipping-lines/lines                       -> shipping-line master registry
    GET  /api/shipping-lines/delivery-orders             -> EDO / CODECO delivery orders
    GET  /api/shipping-lines/messages[/{id}]             -> import ledger (+ row errors)
    GET  /api/shipping-lines/events                      -> shipping-line event poll
    GET  /api/shipping-lines/container/{container_number}-> full view of one box
    GET  /api/shipping-lines/bl/{bill_of_lading}         -> line items by Bill of Lading
    GET  /api/shipping-lines/{shipping_line}             -> all shipments for one line code
    POST /api/shipping-lines/import                      -> import $SHIPPING_LINES_DATA_DIR (idempotent)

RBAC: /api/shipping-lines is restricted to CONTROL_ROOM + CUSTOMS in
gateway/auth.py._POLICY (the customs / cargo clearance audience).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel

from services.shipping_lines import ShippingLinesService, ShippingLinesUploadService

from ..auth import CONTROL_ROOM, Role, auth_enabled

router = APIRouter(prefix="/api/shipping-lines", tags=["shipping-lines"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the performance upload cap
# Roles allowed to upload: control room + customs (+ admin ⊂ control room).
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

_service: Optional[ShippingLinesService] = None
_upload_service: Optional[ShippingLinesUploadService] = None


def get_upload_service(request: Request) -> ShippingLinesUploadService:
    global _upload_service
    if _upload_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _upload_service = ShippingLinesUploadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _upload_service


def require_uploader(request: Request) -> str:
    """Upload write-gate. Reads the auth-middleware principal WITHOUT modifying auth /
    JWT / RBAC (mirrors performance_upload.require_admin). Returns the uploader id for
    the audit. When AUTH_ENABLED is off (dev/mock), the app is open → 'dev'."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "upload_forbidden",
                                    "detail": "Shipping-line upload requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


def _check_list_type(list_type: str) -> str:
    lt = (list_type or "").strip().upper()
    if lt not in ("IAL", "EAL", "EDO"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_list_type", "list_type": list_type,
                                    "allowed": ["IAL", "EAL", "EDO"]})
    return lt


async def _read_upload(file: UploadFile) -> bytes:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "empty_file"})
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={"error": "file_too_large", "max_bytes": _MAX_UPLOAD_BYTES})
    return content


def get_service(request: Request) -> ShippingLinesService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = ShippingLinesService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- DTOs
class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


class ImportTotals(BaseModel):
    files: int
    succeeded: int
    duplicate: int
    failed: int
    records: int
    imported: int


class ImportResponse(BaseModel):
    root: str
    totals: ImportTotals
    results: List[Dict[str, Any]]


def _page(items: List[dict], total: int, limit: int, offset: int, response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


def _norm_list_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("IAL", "EAL"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_list_type", "list_type": value,
                                    "allowed": ["IAL", "EAL"]})
    return v


# ------------------------------------------------------------------- summary
@router.get("/summary", summary="Shipping-line layer dashboard counts")
async def summary(svc: ShippingLinesService = Depends(get_service)) -> Dict[str, Any]:
    return await svc.summary()


# --------------------------------------------------------------------- lines
@router.get("/lines", response_model=Page, summary="Shipping-line master registry")
async def list_lines(
    response: Response,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    items = await svc.list_lines(limit=limit, offset=offset)
    total = await svc.count_lines()
    return _page(items, total, limit, offset, response)


# ----------------------------------------------------------- delivery orders
@router.get("/delivery-orders", response_model=Page, summary="EDO / CODECO delivery orders")
async def list_delivery_orders(
    response: Response,
    container: Optional[str] = None,
    vehicle: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    filters = {"container": container.strip().upper() if container else None,
               "vehicle": vehicle.strip().upper() if vehicle else None}
    items = await svc.list_delivery_orders(filters=filters, limit=limit, offset=offset)
    total = await svc.count_delivery_orders(filters=filters)
    return _page(items, total, limit, offset, response)


# --------------------------------------------------------------- import ledger
@router.get("/messages", response_model=Page, summary="Import ledger (every imported file)")
async def list_messages(
    response: Response,
    list_type: Optional[str] = None,
    terminal: Optional[str] = None,
    import_status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    filters = {"list_type": list_type, "terminal": terminal, "import_status": import_status}
    items = await svc.list_files(filters=filters, limit=limit, offset=offset)
    total = await svc.count_files(filters=filters)
    return _page(items, total, limit, offset, response)


@router.get("/messages/{file_id}", summary="One import-ledger file + its row errors")
async def get_message(file_id: int, svc: ShippingLinesService = Depends(get_service)) -> Dict[str, Any]:
    row = await svc.get_file(file_id, with_errors=True)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "import_file_not_found", "file_id": file_id})
    return row


# -------------------------------------------------------------------- events
@router.get("/events", response_model=Page, summary="Shipping-line event poll")
async def list_events(
    response: Response,
    module: Optional[str] = None,
    container_no: Optional[str] = None,
    event: Optional[str] = None,
    since: Optional[int] = Query(None, description="exclusive lower bound on event id"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    items = await svc.list_events(module=module, container_no=container_no, event=event,
                                  since_id=since, limit=limit, offset=offset)
    return _page(items, len(items), limit, offset, response)


# -------------------------------------------------------------- container view
@router.get("/container/{container_number}", summary="Full shipping-line view of one container")
async def container_view(container_number: str,
                         svc: ShippingLinesService = Depends(get_service)) -> Dict[str, Any]:
    view = await svc.container_view(container_number.strip().upper())
    if not (view["advance_lists"] or view["delivery_orders"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_in_shipping_lines",
                                    "container_no": container_number})
    return view


# ---------------------------------------------------------------- BL lookup
@router.get("/bl/{bill_of_lading}", response_model=Page,
            summary="Advance-list line items by Bill of Lading")
async def by_bill_of_lading(
    bill_of_lading: str,
    response: Response,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    bl = bill_of_lading.strip().upper()
    items = await svc.list_by_bl(bl, limit=limit, offset=offset)
    total = await svc.count_by_bl(bl)
    if total == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "bill_of_lading_not_found", "bill_of_lading": bl})
    return _page(items, total, limit, offset, response)


# --------------------------------------------------------------- list items
@router.get("", response_model=Page, summary="Advance-list line items (filter + page)")
@router.get("/", response_model=Page, include_in_schema=False)
async def list_containers(
    response: Response,
    list_type: Optional[str] = None,
    terminal: Optional[str] = None,
    category: Optional[str] = None,
    freight_kind: Optional[str] = None,
    shipping_line: Optional[str] = None,
    container: Optional[str] = None,
    bl: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    filters = {
        "list_type": _norm_list_type(list_type),
        "terminal": terminal.strip().upper() if terminal else None,
        "category": category.strip().upper() if category else None,
        "freight_kind": freight_kind.strip().upper() if freight_kind else None,
        "shipping_line": shipping_line.strip().upper() if shipping_line else None,
        "container": container.strip().upper() if container else None,
        "bl": bl.strip().upper() if bl else None,
        "q": q.strip() if q else None,
    }
    items = await svc.list_containers(filters=filters, limit=limit, offset=offset)
    total = await svc.count_containers(filters=filters)
    return _page(items, total, limit, offset, response)


# -------------------------------------------------------------------- import
@router.post("/import", response_model=ImportResponse, status_code=status.HTTP_200_OK,
             summary="Import all official customer files under $SHIPPING_LINES_DATA_DIR (idempotent)")
async def import_shipping_lines(svc: ShippingLinesService = Depends(get_service)) -> ImportResponse:
    try:
        summary_ = await svc.import_configured()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "shipping_lines_data_dir_not_found", "path": str(exc)})
    return ImportResponse(**summary_)


# ============================================================ Data Upload sub-module
# Reusable upload workflow (module 4): template → validate (dry-run preview) → confirm
# import. Reuses the SAME tables + ShippingLinesRepository.persist (sha256 + row_sha256
# idempotency). Write-gated to CONTROL_ROOM + CUSTOMS (+ admin ⊂ control room).
@router.get("/templates/{list_type}", summary="Download an upload template (IAL/EAL/EDO)")
async def upload_template(list_type: str, request: Request,
                          svc: ShippingLinesUploadService = Depends(get_upload_service)) -> Response:
    require_uploader(request)
    lt = _check_list_type(list_type)
    csv_text = svc.template(lt)
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="shipping_lines_{lt}_template.csv"'})


@router.post("/validate", summary="Validate an upload (dry-run: parse + preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          list_type: str = Form(...),
                          svc: ShippingLinesUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    lt = _check_list_type(list_type)
    content = await _read_upload(file)
    return await svc.validate(lt, content, file.filename or "upload.csv", uploader)


@router.post("/upload", summary="Import an upload (valid rows persisted; idempotent)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        list_type: str = Form(...),
                        svc: ShippingLinesUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    lt = _check_list_type(list_type)
    content = await _read_upload(file)
    return await svc.import_file(lt, content, file.filename or "upload.csv", uploader)


@router.get("/uploads", response_model=Page, summary="Upload history (import ledger)")
async def upload_history(
    response: Response,
    request: Request,
    list_type: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default="UPLOAD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesUploadService = Depends(get_upload_service),
) -> Page:
    require_uploader(request)
    filters = {"list_type": (list_type.strip().upper() if list_type else None),
               "import_status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One upload with its errors + events")
async def upload_detail(file_id: int, request: Request,
                        svc: ShippingLinesUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res


# -------------------------------------------------- shipping-line shipments (LAST)
# Declared last so the static paths above win over this catch-all {param} route.
@router.get("/{shipping_line}", response_model=Page,
            summary="All advance-list shipments for one shipping-line code")
async def by_shipping_line(
    shipping_line: str,
    response: Response,
    list_type: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: ShippingLinesService = Depends(get_service),
) -> Page:
    code = shipping_line.strip().upper()
    line = await svc.get_line(code)
    if line is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "shipping_line_not_found", "shipping_line": code})
    filters = {"shipping_line": code, "list_type": _norm_list_type(list_type)}
    items = await svc.list_containers(filters=filters, limit=limit, offset=offset)
    total = await svc.count_containers(filters=filters)
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))
