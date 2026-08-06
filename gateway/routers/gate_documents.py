"""/api/gate-docs — Gate Document layer (UC-III: EIR / PIN ticket / Form-13).

A thin router over :class:`services.gate_documents.GateDocumentService`
(service → raw-SQL GateDocumentRepository), in the same mould as
gateway/routers/cfs_ecy.py. It exposes the three client gate documents that
anchor the truck & gate lifecycle plus the Data-Upload triad, and the
document-derived TAT (the corpus's 82/165-min ground truth) which is
independent of the simulator-fed gate-event KPI views.

    GET  /api/gate-docs/summary                    -> module counts
    GET  /api/gate-docs/eir | /pin | /form13       -> list + filter/paginate
    GET  /api/gate-docs/container/{container_no}   -> every document for one box
    GET  /api/gate-docs/truck/{truck_no}           -> every document for one truck
    GET  /api/gate-docs/tat                        -> document-derived TAT summary
    GET  /api/gate-docs/templates/{doc_type}       -> upload template (CSV)
    POST /api/gate-docs/validate | /upload         -> dry-run / confirm import
    GET  /api/gate-docs/uploads[/{file_id}]        -> import ledger (+ row errors)

RBAC: /api/gate-docs is restricted to CONTROL_ROOM + CUSTOMS in
gateway/auth.py._POLICY — the same audience as gate-data and the customs layer.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel

from jnpa_shared.pii import mask_payload

from ..auth import CONTROL_ROOM, Role, auth_enabled
from ..data_mode import data_mode
from ..metrics import REQUESTS
from ..pii import mask_for_request
from services.gate_documents import GateDocumentService
from services.gate_documents.repository import FORM13_SOURCES
from services.gate_documents.upload_parsers import DOC_TYPES, doc_type_ok

router = APIRouter(prefix="/api/gate-docs", tags=["gate-documents"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB — mirrors the other upload modules
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}
#: Longest date window a single query may span (audit finding G1). Bounds the
#: hourly profile at ~2200 buckets so an unbounded range cannot be requested.
_MAX_WINDOW_DAYS = 92


def _check_window(from_date: Optional[date], to_date: Optional[date]) -> None:
    """Validate an optional date window. 400 on an inverted or oversized range."""
    if from_date and to_date:
        if to_date < from_date:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_window",
                                        "message": "to_date must not precede from_date"})
        if (to_date - from_date).days > _MAX_WINDOW_DAYS:
            raise HTTPException(status_code=400,
                                detail={"error": "invalid_window",
                                        "message": f"window must not exceed "
                                                   f"{_MAX_WINDOW_DAYS} days"})

_service: Optional[GateDocumentService] = None


def get_service(request: Request) -> GateDocumentService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = GateDocumentService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def require_uploader(request: Request) -> str:
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "upload_forbidden",
                                    "detail": "gate-document upload requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


def _check_doc_type(doc_type: str) -> str:
    v = doc_type_ok(doc_type)
    if v is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_doc_type", "doc_type": doc_type,
                                    "allowed": list(DOC_TYPES)})
    return v


def _check_source(source: Optional[str]) -> Optional[str]:
    """Validate the optional Form-13 provenance filter (live | sim). Omitted =
    both, so a caller always sees that a document exists and judges it by the
    `source_mode` on each row."""
    if source is None or source == "":
        return None
    v = source.strip().lower()
    if v == "all":
        return None
    if v not in FORM13_SOURCES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_source", "source": source,
                                    "allowed": [*FORM13_SOURCES, "all"]})
    return v


def _norm_plate(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


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


# ------------------------------------------------------------------- summary
@router.get("/summary", summary="Gate-document module counts")
async def summary(svc: GateDocumentService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("gate_docs", "ok").inc()
    return await svc.summary()


# --------------------------------------------------------------------- lists
async def _list(svc: GateDocumentService, doc_type: str, response: Response,
                filters: dict, limit: int, offset: int,
                request: Optional[Request] = None) -> Page:
    """Shared list path for EIR / PIN / Form-13.

    Gate documents carry ``driver_licence`` (a real DL number transcribed off the
    physical slip), so the page is routed through the PII gate before it is
    serialised. ``request`` is optional so any existing in-process caller that
    does not pass one still works — but then nothing is unmasked, which is the
    safe direction.
    """
    res = await svc.list_docs(doc_type, filters=filters, limit=limit, offset=offset)
    REQUESTS.labels("gate_docs", "ok").inc()
    items = res["items"]
    if request is not None:
        items = mask_for_request(request, items, surface=f"gate_docs.{doc_type.lower()}")
    else:
        items = mask_payload(items)
    return _page(items, res["total"], limit, offset, response)


@router.get("/eir", response_model=Page, summary="Equipment Interchange Reports")
async def list_eir(
    request: Request,
    response: Response,
    container: Optional[str] = None,
    truck: Optional[str] = None,
    terminal: Optional[str] = None,
    from_date: Optional[date] = Query(
        None, description="Only EIRs whose truck_in_time falls on/after this date"),
    to_date: Optional[date] = Query(
        None, description="Only EIRs whose truck_in_time falls on/before this date"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    data_origin: Optional[str] = Depends(data_mode),
    svc: GateDocumentService = Depends(get_service),
) -> Page:
    """List EIRs, optionally over a date window.

    ``from_date`` / ``to_date`` (audit finding G1) make the real gate-arrival
    history queryable through the API. Without them a JNPA what-if answer about
    1-3 August could only be produced by raw SQL, which fails the Notice's
    requirement to cite the API queries the working rests on."""
    _check_window(from_date, to_date)
    filters = {"container_number": container,
               "truck_no": _norm_plate(truck) if truck else None, "terminal": terminal,
               "from_date": from_date, "to_date": to_date,
               "data_origin": data_origin}
    return await _list(svc, "EIR", response, filters, limit, offset, request)


@router.get("/eir/profile",
            summary="EIR gate-arrival counts bucketed by hour or day")
async def eir_profile(
    from_date: date = Query(..., description="Window start (inclusive)"),
    to_date: date = Query(..., description="Window end (inclusive)"),
    terminal: Optional[str] = None,
    truck: Optional[str] = None,
    group_by: str = Query("hour", pattern="^(hour|day)$"),
    data_origin: Optional[str] = Depends(data_mode),
    svc: GateDocumentService = Depends(get_service),
) -> Dict[str, Any]:
    """Gate arrivals per hour (or per day) over an arbitrary historical window,
    counted from ``core.eir.truck_in_time``.

    The aggregate counterpart of GET /api/gate-docs/eir — same filters, same
    rows, counted instead of paged. ``GET /api/gate/hourly-profile`` answers the
    same question for the simulation layer and falls back to ``core.gate_event``;
    this one stays strictly within the gate-document module."""
    _check_window(from_date, to_date)
    filters = {"terminal": terminal,
               "truck_no": _norm_plate(truck) if truck else None,
               "from_date": from_date, "to_date": to_date,
               "data_origin": data_origin}
    result = await svc.hourly_profile("EIR", filters=filters, group_by=group_by)
    REQUESTS.labels("gate_docs", "ok").inc()
    return {"window": {"from": from_date.isoformat(), "to": to_date.isoformat()},
            "source": "core.eir.truck_in_time", **result}


@router.get("/pin", response_model=Page, summary="PIN tickets (one row per move leg)")
async def list_pin(
    request: Request,
    response: Response,
    pin: Optional[str] = None,
    container: Optional[str] = None,
    truck: Optional[str] = None,
    terminal: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    data_origin: Optional[str] = Depends(data_mode),
    svc: GateDocumentService = Depends(get_service),
) -> Page:
    filters = {"pin_number": pin, "container_number": container,
               "truck_no": _norm_plate(truck) if truck else None, "terminal": terminal,
               "data_origin": data_origin}
    return await _list(svc, "PIN", response, filters, limit, offset, request)


@router.get("/form13", response_model=Page, summary="Form 13 gate documents")
async def list_form13(
    request: Request,
    response: Response,
    visit_id: Optional[str] = None,
    container: Optional[str] = None,
    vehicle: Optional[str] = None,
    terminal: Optional[str] = None,
    source: Optional[str] = Query(None, description="provenance filter: live | sim | all"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    svc: GateDocumentService = Depends(get_service),
) -> Page:
    filters = {"visit_id": visit_id, "container_number": container,
               "truck_no": _norm_plate(vehicle) if vehicle else None, "terminal": terminal,
               "source": _check_source(source)}
    return await _list(svc, "FORM13", response, filters, limit, offset, request)


@router.get("/documents", response_model=Page,
            summary="Parsed source gate documents (Form 13 / EIR / PIN, as filed)")
async def list_source_documents(
    response: Response,
    category: Optional[str] = Query(None, description="FORM13 | EIR | PIN_TICKET"),
    container: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    svc: GateDocumentService = Depends(get_service),
) -> Page:
    """The customer's own gate documents, parsed verbatim from the shared corpus.

    NOT the same store as `/form13`, which reads `core.gate_capture` — that is
    202/203 seeded rows. These are the 13 real parsed documents (Form 13, EIR and
    PIN tickets) with their full as-filed payload in `attrs`. Read-only.
    """
    res = await svc.list_source_documents(
        category=category, container=container, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


# ------------------------------------------------------------ cross-doc views
@router.get("/container/{container_no}", summary="Every gate document for one container")
async def docs_for_container(
    request: Request,
    container_no: str,
    source: Optional[str] = Query(None, description="Form-13 provenance filter: live | sim | all (default all)"),
    svc: GateDocumentService = Depends(get_service),
) -> Dict[str, Any]:
    res = await svc.docs_for_container(container_no.strip().upper(),
                                       source=_check_source(source))
    REQUESTS.labels("gate_docs", "ok").inc()
    return mask_for_request(request, res, surface="gate_docs.container")


@router.get("/truck/{truck_no}", summary="Every gate document for one truck (incl. containerless)")
async def docs_for_truck(
    request: Request,
    truck_no: str,
    source: Optional[str] = Query(None, description="Form-13 provenance filter: live | sim | all (default all)"),
    svc: GateDocumentService = Depends(get_service),
) -> Dict[str, Any]:
    res = await svc.docs_for_truck(_norm_plate(truck_no), source=_check_source(source))
    REQUESTS.labels("gate_docs", "ok").inc()
    return mask_for_request(request, res, surface="gate_docs.truck")


@router.get("/tat", summary="Document-derived truck turnaround time (EIR TruckIn -> TruckOut)")
async def tat_summary(terminal: Optional[str] = None,
                      svc: GateDocumentService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("gate_docs", "ok").inc()
    return await svc.tat_summary(terminal=terminal)


# ============================================================ Data Upload sub-module
@router.get("/templates/{doc_type}", summary="Download a gate-document upload template")
async def upload_template(doc_type: str, request: Request,
                          svc: GateDocumentService = Depends(get_service)) -> Response:
    require_uploader(request)
    dt = _check_doc_type(doc_type)
    return Response(content=svc.template(dt), media_type="text/csv",
                    headers={"Content-Disposition":
                             f'attachment; filename="gate_doc_{dt.lower()}_template.csv"'})


@router.post("/validate", summary="Validate a gate-document upload (dry-run preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          doc_type: str = Form(...),
                          svc: GateDocumentService = Depends(get_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    dt = _check_doc_type(doc_type)
    content = await _read_upload(file)
    return await svc.validate(dt, content, file.filename or "upload.csv", uploader)


@router.post("/upload", summary="Import a gate-document upload (idempotent by row hash)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        doc_type: str = Form(...),
                        svc: GateDocumentService = Depends(get_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    dt = _check_doc_type(doc_type)
    content = await _read_upload(file)
    return await svc.import_file(dt, content, file.filename or "upload.csv", uploader)


@router.get("/uploads", response_model=Page, summary="Gate-document upload history")
async def upload_history(
    response: Response,
    request: Request,
    doc_type: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default="UPLOAD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: GateDocumentService = Depends(get_service),
) -> Page:
    require_uploader(request)
    filters = {"doc_type": (_check_doc_type(doc_type) if doc_type else None),
               "import_status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One gate-document upload with its row errors")
async def upload_detail(file_id: int, request: Request,
                        svc: GateDocumentService = Depends(get_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res
