"""/api/customs — Customs document layer (module 5: IGM/OOC/SMTP/RMS/LEO/Shipping Bill).

A thin router over :class:`services.customs.CustomsService` (service → raw-SQL
CustomsRepository), in the same mould as gateway/routers/cfs_ecy.py. It exposes the
customs documents imported from the OFFICIAL JNPA customer files (migration 0031)
plus an admin import trigger, and cross-links a container to every customs document
that references it via the mart.v_customs_container_status view — a soft, by-value
join to core.cargo. It touches no existing table.

    GET  /api/customs/summary                       -> dashboard counts
    GET  /api/customs/messages[/{id}]               -> import ledger (+ row errors)
    GET  /api/customs/igm[/{igm_no}/containers]     -> import manifests + containers
    GET  /api/customs/ooc | /smtp | /rms | /leo | /shipping-bills
    GET  /api/customs/containers/{container_no}      -> full customs view of one box
    GET  /api/customs/events                         -> customs event poll
    POST /api/customs/import                         -> import $CUSTOMS_DATA_DIR (idempotent)

RBAC: /api/customs is restricted to CONTROL_ROOM + CUSTOMS in gateway/auth.py._POLICY
(the customs clearance pipeline audience) — reads and the import write alike.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..datewindow import DateWindow, date_window
from fastapi import (
    APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status,
)
from pydantic import BaseModel

from gateway.data_mode import data_mode
from gateway.upload_limits import MAX_UPLOAD_BYTES
from services.customs import CustomsService

router = APIRouter(prefix="/api/customs", tags=["customs"])

_service: Optional[CustomsService] = None


def get_service(request: Request) -> CustomsService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CustomsService(dsn=getattr(cfg, "postgres_dsn", None) or None)
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
    # Auto-reconcile outcome (customs facts -> core.cargo.customs_status); None
    # when nothing new was imported or reconcile was skipped.
    reconcile: Optional[Dict[str, Any]] = None


def _page(items: List[dict], total: int, limit: int, offset: int, response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


# ------------------------------------------------------------------- summary
@router.get("/summary", summary="Customs layer dashboard counts")
async def summary(svc: CustomsService = Depends(get_service),
                  mode: Optional[str] = Depends(data_mode)) -> Dict[str, Any]:
    return await svc.summary(data_origin=mode)


# ------------------------------------------------------------------- messages
@router.get("/messages", response_model=Page, summary="Import ledger (every imported file)")
async def list_messages(
    response: Response,
    module: Optional[str] = None,
    message_type: Optional[str] = None,
    import_status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"module": module, "message_type": message_type, "import_status": import_status,
               "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "created_at"

    items = await svc.list_messages(filters=filters, limit=limit, offset=offset)
    total = await svc.count_messages(filters=filters)
    return _page(items, total, limit, offset, response)


@router.get("/messages/{message_id}", summary="One import-ledger message + its row errors")
async def get_message(message_id: int, svc: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    msg = await svc.get_message(message_id, with_errors=True)
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "message_not_found", "message_id": message_id})
    return msg


# ----------------------------------------------------------------------- IGM
@router.get("/igm", response_model=Page, summary="Import General Manifests (CHPOI03)")
async def list_igm(
    response: Response,
    igm_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"igm_no": igm_no, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "igm_date"

    items = await svc.list_igm(filters=filters, limit=limit, offset=offset)
    total = await svc.count_igm(filters=filters)
    return _page(items, total, limit, offset, response)


@router.get("/igm/{igm_no}/containers", response_model=Page,
            summary="Containers declared on an IGM")
async def list_igm_containers(
    igm_no: str,
    response: Response,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
) -> Page:
    filters = {"igm_no": igm_no, "data_origin": mode}
    items = await svc.list_igm_containers(filters=filters, limit=limit, offset=offset)
    total = await svc.count_igm_containers(filters=filters)
    return _page(items, total, limit, offset, response)


# ----------------------------------------------------------------------- OOC
@router.get("/ooc", response_model=Page, summary="Out-Of-Charge / Bill-of-Entry (CHPOI10)")
async def list_ooc(
    response: Response,
    bill_of_entry_no: Optional[str] = None,
    igm_no: Optional[str] = None,
    out_of_charge_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"bill_of_entry_no": bill_of_entry_no, "igm_no": igm_no,
               "out_of_charge_no": out_of_charge_no, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "created_at"

    items = await svc.list_ooc(filters=filters, limit=limit, offset=offset)
    total = await svc.count_ooc(filters=filters)
    return _page(items, total, limit, offset, response)


@router.get("/ooc/{be_no}/items", summary="One Bill of Entry: OOC facts, containers and invoice items")
async def ooc_detail(be_no: str, svc: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    """Everything behind one BE — the out-of-charge grant, the containers it covers
    and every invoice line item (description, HS code, CIF/assessable value)."""
    view = await svc.ooc_detail(be_no)
    if view.get("ooc") is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "bill_of_entry_not_found", "be_no": be_no})
    return view


# ---------------------------------------------------------------------- SMTP
@router.get("/smtp", response_model=Page, summary="Sub-Manifest Transhipment Permits (CHPOI13)")
async def list_smtp(
    response: Response,
    smtp_no: Optional[str] = None,
    igm_no: Optional[str] = None,
    bond_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"smtp_no": smtp_no, "igm_no": igm_no, "bond_no": bond_no, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "created_at"

    items = await svc.list_smtp(filters=filters, limit=limit, offset=offset)
    total = await svc.count_smtp(filters=filters)
    return _page(items, total, limit, offset, response)


# ----------------------------------------------------------------------- RMS
@router.get("/rms", response_model=Page, summary="RMS container scanning selection lists")
async def list_rms(
    response: Response,
    igm_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"igm_no": igm_no, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "igm_date"

    items = await svc.list_rms(filters=filters, limit=limit, offset=offset)
    total = await svc.count_rms(filters=filters)
    return _page(items, total, limit, offset, response)


@router.get("/rms/{igm_no}/containers", response_model=Page,
            summary="Selected containers of an RMS scan list (scanner routing facts)")
async def list_rms_containers(
    igm_no: str,
    response: Response,
    machine_type: Optional[str] = Query(None, description="scanner machine class: D (drive-through) / M (mobile) / F (fixed)"),
    scan_location: Optional[str] = Query(None, description="scanner location code contains-match, e.g. INNSA1RSDT02"),
    container_no: Optional[str] = None,
    limit: int = Query(200, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
) -> Page:
    if not str(igm_no).strip().isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_igm_no", "igm_no": igm_no,
                                    "detail": "igm_no must be numeric"})
    filters = {"igm_no": igm_no, "machine_type": machine_type,
               "scan_location": scan_location, "container_no": container_no,
               "data_origin": mode}
    items = await svc.list_rms_containers(filters=filters, limit=limit, offset=offset)
    total = await svc.count_rms_containers(filters=filters)
    return _page(items, total, limit, offset, response)


# ----------------------------------------------------------------------- LEO
@router.get("/leo", response_model=Page, summary="Let Export Orders")
async def list_leo(
    response: Response,
    sb_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"sb_no": sb_no, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "leo_date"

    items = await svc.list_leo(filters=filters, limit=limit, offset=offset)
    total = await svc.count_leo(filters=filters)
    return _page(items, total, limit, offset, response)


# -------------------------------------------------------------- Shipping Bill
@router.get("/shipping-bills", response_model=Page, summary="Shipping Bills (export declarations)")
async def list_shipping_bills(
    response: Response,
    sb_no: Optional[str] = None,
    site_id: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
    window: DateWindow = Depends(date_window),
) -> Page:
    filters = {"sb_no": sb_no, "site_id": site_id, "data_origin": mode}
    # GAP-DATE-01: the window travels with the filters; the column is
    # stated here, never inferred by the shared where-builder.
    filters["_window"] = window
    filters["_date_col"] = "sb_date"

    items = await svc.list_shipping_bills(filters=filters, limit=limit, offset=offset)
    total = await svc.count_shipping_bills(filters=filters)
    return _page(items, total, limit, offset, response)


# ------------------------------------------------------ container customs view
@router.get("/containers/{container_no}", summary="Full customs view of one container")
async def container_customs(container_no: str,
                            svc: CustomsService = Depends(get_service),
                            mode: Optional[str] = Depends(data_mode)) -> Dict[str, Any]:
    view = await svc.container_customs(container_no.strip().upper(), data_origin=mode)
    if not (view["igm"] or view["ooc"] or view["smtp"] or view["rms"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_in_customs", "container_no": container_no})
    return view


# -------------------------------------------------------------------- events
@router.get("/events", response_model=Page, summary="Customs event poll")
async def list_events(
    response: Response,
    module: Optional[str] = None,
    container_no: Optional[str] = None,
    event: Optional[str] = None,
    since: Optional[int] = Query(None, description="exclusive lower bound on event id"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
    mode: Optional[str] = Depends(data_mode),
) -> Page:
    items = await svc.list_events(module=module, container_no=container_no, event=event,
                                  since_id=since, data_origin=mode, limit=limit, offset=offset)
    return _page(items, len(items), limit, offset, response)


# -------------------------------------------------------------------- import
@router.post("/import", response_model=ImportResponse, status_code=status.HTTP_200_OK,
             summary="Import all official customer files under $CUSTOMS_DATA_DIR (idempotent)")
async def import_customs(svc: CustomsService = Depends(get_service)) -> ImportResponse:
    try:
        summary = await svc.import_configured()
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "customs_data_dir_not_found", "path": str(exc)})
    return ImportResponse(**summary)


@router.post("/reconcile", summary="Bind customs docs to cargo lifecycle (customs_status)")
async def reconcile(svc: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    """Apply the customs -> cargo workflow: Out-Of-Charge marks the box CLEARED, an RMS
    scan selection marks it UNDER_INSPECTION — only for containers already in core.cargo.
    Idempotent; emits customs events + raises scan-hold notifications on the existing feed."""
    return await svc.reconcile_cargo()


@router.post("/materialize", summary="Create cargo rows for manifested containers (IGM -> Cargo)")
async def materialize(
    igm_no: Optional[str] = Query(default=None,
                                  description="restrict to one IGM; omit for every manifest"),
    limit: int = Query(default=5000, ge=1, le=20000),
    reconcile_after: bool = Query(default=True,
                                  description="also run /reconcile so customs_status binds"),
    svc: CustomsService = Depends(get_service),
) -> Dict[str, Any]:
    """Close the IGM -> Cargo gap: give every manifested container a cargo row.

    Without this step the import lifecycle could not start — core.igm_line_container
    held thousands of real manifested boxes while core.cargo held a handful, so
    ``/reconcile`` (which only updates rows that already exist) had nothing to
    bind and no container could be discharged, yard-assigned, verified or
    released.

    Idempotent: rows are inserted ON CONFLICT DO NOTHING, so a second call creates
    nothing and never disturbs a container already moving through the yard. Every
    new row starts at ``CREATED``/``PENDING`` — the state machine is still walked
    step by step, nothing is fast-forwarded.
    """
    return await svc.materialize_cargo(igm_no=igm_no, limit=limit,
                                       reconcile=reconcile_after)


# --------------------------------------------------------------------- upload
# UC2-036: the customs corpus had no browser upload path at all.
#
# `POST /api/customs/import` re-scans a server-side directory ($CUSTOMS_DATA_DIR),
# so it is an admin action — it cannot ingest a file an operator is holding. That
# left the one document family the demo leads with (IGM / OOC / SMTP) as the only
# module with no way to show a file reaching the screen.
#
# This reuses `CustomsService.import_bytes` verbatim — the SAME path the JNPA
# Port-Data sync uses — so sha256 dedup, the import ledger and event emission
# behave identically to an API-delivered file. The only difference is provenance:
# `uploaded_by` is NOT 'jnpa-api' here, so rows are tagged `data_origin='MANUAL'`
# (DEMO) and a browser upload can never masquerade as the live feed.

@router.post("/upload", summary="Import one customs file (IGM/OOC/SMTP/RMS/LEO/SB)")
async def customs_upload(request: Request,
                         file: UploadFile = File(...),
                         service: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    """Ingest a single customs document.

    Idempotent by content hash: re-uploading the same bytes is recognised by the
    import ledger and does not duplicate rows, so a nervous double-click during a
    demo is harmless.

    ⚠ There is no dry-run. Every other ingest module offers `/validate` first,
    but the customs service has no parse-without-persist path, and faking one by
    importing then deleting would leave ledger and event rows behind. Rather than
    present a preview this endpoint cannot honour, it imports directly and says
    so — `dry_run_supported: false` is in the response for callers that branch
    on it.
    """
    content = await file.read()
    if not content:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "empty_file"})
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={"error": "file_too_large",
                                    "max_bytes": MAX_UPLOAD_BYTES})

    filename = file.filename or "upload.xml"
    # Customs format detection routes on the FILENAME (CHPOI03/10/13 prefixes,
    # .TXT, .XLSX header probe), so an unnamed upload cannot be classified.
    result = await service.import_bytes(content, filename, uploaded_by=_uploader(request))
    return {"filename": filename, "bytes": len(content),
            "dry_run_supported": False, **(result or {})}


def _uploader(request: Request) -> str:
    """Who to attribute the upload to — anything but 'jnpa-api', which is the
    reserved token that tags rows as LIVE."""
    principal = getattr(getattr(request, "state", None), "principal", None)
    who = getattr(principal, "sub", None) or "operator"
    return "operator" if who.lower() == "jnpa-api" else who
