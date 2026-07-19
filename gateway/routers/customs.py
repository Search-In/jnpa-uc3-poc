"""/api/customs — Customs document layer (module 5: IGM/OOC/SMTP/RMS/LEO/Shipping Bill).

A thin router over :class:`services.customs.CustomsService` (service → raw-SQL
CustomsRepository), in the same mould as gateway/routers/cfs_ecy.py. It exposes the
customs documents imported from the OFFICIAL JNPA customer files (migration 0031)
plus an admin import trigger, and cross-links a container to every customs document
that references it via the jnpa.v_customs_container_status view — a soft, by-value
join to jnpa.cargo. It touches no existing table.

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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

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


def _page(items: List[dict], total: int, limit: int, offset: int, response: Response) -> Page:
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


# ------------------------------------------------------------------- summary
@router.get("/summary", summary="Customs layer dashboard counts")
async def summary(svc: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    return await svc.summary()


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
) -> Page:
    filters = {"module": module, "message_type": message_type, "import_status": import_status}
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
) -> Page:
    filters = {"igm_no": igm_no}
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
) -> Page:
    filters = {"igm_no": igm_no}
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
) -> Page:
    filters = {"bill_of_entry_no": bill_of_entry_no, "igm_no": igm_no,
               "out_of_charge_no": out_of_charge_no}
    items = await svc.list_ooc(filters=filters, limit=limit, offset=offset)
    total = await svc.count_ooc(filters=filters)
    return _page(items, total, limit, offset, response)


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
) -> Page:
    filters = {"smtp_no": smtp_no, "igm_no": igm_no, "bond_no": bond_no}
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
) -> Page:
    filters = {"igm_no": igm_no}
    items = await svc.list_rms(filters=filters, limit=limit, offset=offset)
    total = await svc.count_rms(filters=filters)
    return _page(items, total, limit, offset, response)


# ----------------------------------------------------------------------- LEO
@router.get("/leo", response_model=Page, summary="Let Export Orders")
async def list_leo(
    response: Response,
    sb_no: Optional[str] = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: CustomsService = Depends(get_service),
) -> Page:
    filters = {"sb_no": sb_no}
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
) -> Page:
    filters = {"sb_no": sb_no, "site_id": site_id}
    items = await svc.list_shipping_bills(filters=filters, limit=limit, offset=offset)
    total = await svc.count_shipping_bills(filters=filters)
    return _page(items, total, limit, offset, response)


# ------------------------------------------------------ container customs view
@router.get("/containers/{container_no}", summary="Full customs view of one container")
async def container_customs(container_no: str,
                            svc: CustomsService = Depends(get_service)) -> Dict[str, Any]:
    view = await svc.container_customs(container_no.strip().upper())
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
) -> Page:
    items = await svc.list_events(module=module, container_no=container_no, event=event,
                                  since_id=since, limit=limit, offset=offset)
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
    scan selection marks it UNDER_INSPECTION — only for containers already in jnpa.cargo.
    Idempotent; emits customs events + raises scan-hold notifications on the existing feed."""
    return await svc.reconcile_cargo()
