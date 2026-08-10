"""/api/cfs-ecy — CFS-ECY CODECO gate movements (UC-III module 13, additive, read-only).

A thin router over :class:`services.cfs_ecy.CfsEcyService` (CfsEcyService →
raw-SQL CfsEcyRepository), in the same mould as gateway/routers/drivers_master.py.
It reads the off-dock gate-movement feed (core.cfs_ecy_movement + the derived
mart.v_cfs_ecy_dwell view) and enriches a container timeline with the EXISTING
Container Lifecycle status via a soft, best-effort read of core.cargo. It writes
nothing and touches no existing table — auth / JWT / RBAC / cargo / vehicle /
driver / transporter are all untouched.

    GET /api/cfs-ecy/movements                    -> list + filter/search/paginate
    GET /api/cfs-ecy/stats                         -> KPI aggregates + daily throughput
    GET /api/cfs-ecy/dwell                         -> CFS dwell report
    GET /api/cfs-ecy/containers/{container_number} -> CODECO timeline + dwell + cargo status

UC3-003 adds a second, read-only group over core.container_event (the imported
CFS/ECY CODECO gate log) for KPI 3, "TRT for empty containers from ECD":

    GET /api/cfs-ecy/events                        -> raw gate events (filterable)
    GET /api/cfs-ecy/empty-trt                     -> the KPI + its provenance/anomalies
    GET /api/cfs-ecy/empty-trt/chains              -> per-container lifecycles
    GET /api/cfs-ecy/empty-trt/anomalies/{code}    -> containers behind one finding
    GET /api/cfs-ecy/empty-trt/containers/{cn}     -> one container end to end

RBAC: /api/cfs-ecy is not in gateway/auth.py._POLICY, so it inherits the default
"any authenticated role" rule (read-only). No auth change is required or made.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query, Request,
                     Response, UploadFile, status)
from pydantic import BaseModel, ConfigDict

from ..auth import CONTROL_ROOM, Role, auth_enabled
from ..data_mode import data_mode
from ..metrics import REQUESTS
from services.cfs_ecy import (CfsEcyService, CfsEcyUploadService, EcyCfsChainService,
                              EmptyTrtService)
from services.cfs_ecy.trt_repository import CODECO_EVENT_TYPES as _CODECO_EVENT_TYPES

router = APIRouter(prefix="/api/cfs-ecy", tags=["cfs-ecy"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the shipping-lines upload cap
# Roles allowed to upload: control room + customs (+ admin ⊂ control room).
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

_service: Optional[CfsEcyService] = None
_upload_service: Optional[CfsEcyUploadService] = None
_chain_service: Optional[EcyCfsChainService] = None


def get_chain_service(request: Request) -> EcyCfsChainService:
    global _chain_service
    if _chain_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _chain_service = EcyCfsChainService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _chain_service


def get_service(request: Request) -> CfsEcyService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CfsEcyService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def get_upload_service(request: Request) -> CfsEcyUploadService:
    global _upload_service
    if _upload_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _upload_service = CfsEcyUploadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _upload_service


def require_uploader(request: Request) -> str:
    """Upload write-gate. Reads the auth-middleware principal WITHOUT modifying auth /
    JWT / RBAC (mirrors shipping_lines.require_uploader). Returns the uploader id for
    the audit. When AUTH_ENABLED is off (dev/mock), the app is open → 'dev'."""
    if not auth_enabled():
        return "dev"
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    if principal is None or role not in _UPLOADER_ROLES:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail={"error": "upload_forbidden",
                                    "detail": "CFS-ECY upload requires CONTROL_ROOM, CUSTOMS or ADMIN"})
    return getattr(principal, "sub", "uploader")


def _check_facility(facility: str) -> str:
    v = (facility or "").strip().upper()
    if v not in ("CFS", "ECY"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_facility", "facility": facility,
                                    "allowed": ["CFS", "ECY"]})
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


# --------------------------------------------------------------------- DTOs
class MovementOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Optional[int] = None
    facility_type: Optional[str] = None
    container_number: Optional[str] = None
    iso_valid: Optional[bool] = None
    event_ts: Optional[datetime] = None
    mode: Optional[str] = None
    source: Optional[str] = None
    source_file: Optional[str] = None
    created_at: Optional[datetime] = None


class MovementListResponse(BaseModel):
    items: List[MovementOut]
    total: int
    limit: int
    offset: int
    count: int


class DailyThroughput(BaseModel):
    day: str
    in_count: int
    out_count: int


class StatsOut(BaseModel):
    total_in: int
    total_out: int
    total_events: int
    container_count: int
    active_containers: int
    iso_invalid: int
    average_dwell_hours: Optional[float] = None
    median_dwell_hours: Optional[float] = None
    dwell_count: int
    daily_throughput: List[DailyThroughput]


class DwellItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    container_number: Optional[str] = None
    facility_type: Optional[str] = None
    first_in_ts: Optional[datetime] = None
    last_out_ts: Optional[datetime] = None
    in_events: Optional[int] = None
    out_events: Optional[int] = None
    dwell_hours: Optional[float] = None


class DwellResponse(BaseModel):
    items: List[DwellItem]
    total: int
    limit: int
    offset: int
    count: int
    summary: Dict[str, Any]
    note: str


# ------------------------------------------------------------------- helpers
def _facility(value: Optional[str]) -> Optional[str]:
    """Normalize + validate the facility filter to CFS / ECY (else 400)."""
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("CFS", "ECY"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_facility", "facility": value})
    return v


def _mode(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("IN", "OUT"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_mode", "mode": value})
    return v


def _filters(facility, mode, container, date_from, date_to,
             data_origin=None) -> Dict[str, Any]:
    return {
        "facility_type": _facility(facility),
        "mode": _mode(mode),
        "container": container,
        "ts_from": date_from,
        "ts_to": date_to,
        "data_origin": data_origin,
    }


# ------------------------------------------------------------------- endpoints
@router.get("/movements", response_model=MovementListResponse,
            summary="List / search CFS-ECY CODECO gate movements")
async def list_movements(
    facility: Optional[str] = Query(default=None, description="CFS | ECY"),
    mode: Optional[str] = Query(default=None, description="IN | OUT"),
    container: Optional[str] = Query(default=None, description="container number contains"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    sort: str = Query(default="event_ts"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    data_origin: Optional[str] = Depends(data_mode),
    service: CfsEcyService = Depends(get_service),
) -> MovementListResponse:
    filters = _filters(facility, mode, container, date_from, date_to, data_origin)
    res = await service.list_movements(filters, sort=sort, direction=direction,
                                       limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return MovementListResponse(**res)


@router.get("/stats", response_model=StatsOut, summary="CFS-ECY KPI aggregates + daily throughput")
async def stats(
    facility: Optional[str] = Query(default=None, description="CFS | ECY"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    data_origin: Optional[str] = Depends(data_mode),
    service: CfsEcyService = Depends(get_service),
) -> StatsOut:
    filters = _filters(facility, None, None, date_from, date_to, data_origin)
    res = await service.stats(filters)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return StatsOut(**res)


@router.get("/dwell", response_model=DwellResponse, summary="CFS dwell report (OUT - IN)")
async def dwell(
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    data_origin: Optional[str] = Depends(data_mode),
    service: CfsEcyService = Depends(get_service),
) -> DwellResponse:
    filters = {"ts_from": date_from, "ts_to": date_to, "data_origin": data_origin}
    res = await service.dwell_report(filters, limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return DwellResponse(**res)


@router.get("/containers/{container_number}",
            summary="CODECO timeline + dwell + cargo lifecycle status for one container")
async def container_timeline(container_number: str,
                             service: CfsEcyService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.container_timeline(container_number)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_found",
                                    "container_number": container_number})
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res


# ==================================================== ECY→CFS chain (F-Y1 lifecycle)
# The empty-repositioning chain materialised into core.ecy_cfs_chain (migration
# 0114): ECY gate-out -> road shuttle -> CFS gate-in -> dwell -> CFS gate-out,
# with transit/cycle durations and anomaly flags. Before this the chain existed
# only in the operator's head — the dwell view groups BY facility, so an ECY leg
# and a CFS leg could never combine.
@router.post("/chains/rebuild", summary="Rebuild the ECY→CFS chains from CODECO movements")
async def rebuild_chains(request: Request,
                         svc: EcyCfsChainService = Depends(get_chain_service)) -> Dict[str, Any]:
    require_uploader(request)          # a write action: same gate as the uploads
    res = await svc.rebuild()
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res


@router.get("/chains", response_model=Page, summary="ECY→CFS repositioning chains")
async def list_chains(
    response: Response,
    container: Optional[str] = None,
    chain_status: Optional[str] = Query(None, description="COMPLETE | PARTIAL | ORPHAN"),
    anomaly_only: bool = False,
    anomaly_code: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    data_origin: Optional[str] = Depends(data_mode),
    svc: EcyCfsChainService = Depends(get_chain_service),
) -> Page:
    filters = {"container_number": container, "chain_status": chain_status,
               "anomaly_only": anomaly_only, "anomaly_code": anomaly_code,
               "data_origin": data_origin}
    res = await svc.list_chains(filters, limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/chains/stats", summary="Chain KPIs: completeness, transit/dwell/cycle, anomalies")
async def chain_stats(svc: EcyCfsChainService = Depends(get_chain_service)) -> Dict[str, Any]:
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return await svc.stats()


@router.get("/chains/{container_number}", summary="One container's full repositioning chain")
async def get_chain(container_number: str,
                    svc: EcyCfsChainService = Depends(get_chain_service)) -> Dict[str, Any]:
    res = await svc.get_chain(container_number)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "chain_not_found",
                                    "container_number": container_number.strip().upper()})
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res


# ============================================================ Data Upload sub-module
# Reusable upload workflow (module 13): template → validate (dry-run preview) → confirm
# import. Reuses the SAME core.cfs_ecy_movement table + its (facility_type,
# container_number, event_ts, mode) UNIQUE key (idempotent). Write-gated to
# CONTROL_ROOM + CUSTOMS (+ admin ⊂ control room). Facility (CFS/ECY) comes from the
# selector — it is not a column in the JNPA CODECO files.
@router.get("/templates/{facility}", summary="Download a CFS-ECY upload template")
async def upload_template(facility: str, request: Request,
                          svc: CfsEcyUploadService = Depends(get_upload_service)) -> Response:
    require_uploader(request)
    fac = _check_facility(facility)
    csv_text = svc.template()
    return Response(content=csv_text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="cfs_ecy_{fac}_template.csv"'})


@router.post("/validate", summary="Validate a CFS-ECY upload (dry-run: parse + preview, no import)")
async def upload_validate(request: Request,
                          file: UploadFile = File(...),
                          facility: str = Form(...),
                          svc: CfsEcyUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    fac = _check_facility(facility)
    content = await _read_upload(file)
    return await svc.validate(fac, content, file.filename or "upload.csv", uploader)


@router.post("/upload", summary="Import a CFS-ECY upload (valid rows persisted; idempotent)")
async def upload_import(request: Request,
                        file: UploadFile = File(...),
                        facility: str = Form(...),
                        svc: CfsEcyUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    uploader = require_uploader(request)
    fac = _check_facility(facility)
    content = await _read_upload(file)
    return await svc.import_file(fac, content, file.filename or "upload.csv", uploader)


@router.get("/uploads", response_model=Page, summary="CFS-ECY upload history (import ledger)")
async def upload_history(
    response: Response,
    request: Request,
    facility: Optional[str] = None,
    status_: Optional[str] = Query(default=None, alias="status"),
    source: Optional[str] = Query(default="UPLOAD"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    svc: CfsEcyUploadService = Depends(get_upload_service),
) -> Page:
    require_uploader(request)
    filters = {"facility_type": (facility.strip().upper() if facility else None),
               "import_status": status_, "source": (source or None)}
    res = await svc.list_uploads(filters, limit=limit, offset=offset)
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/uploads/{file_id}", summary="One CFS-ECY upload with its row errors")
async def upload_detail(file_id: int, request: Request,
                        svc: CfsEcyUploadService = Depends(get_upload_service)) -> Dict[str, Any]:
    require_uploader(request)
    res = await svc.get_upload(file_id)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "upload_not_found", "file_id": file_id})
    return res


# ================================================== UC3-003 — empty-container TRT
# KPI 3, "TRT for empty containers from ECD", over the REAL CFS/ECY CODECO gate
# logs held in core.container_event (loaded by scripts/import_uc3_003_cfs_ecy.py,
# scored by mart.v_empty_container_trt from migration 0133).
#
# These endpoints are a SEPARATE read path from /movements and /chains above:
# those serve core.cfs_ecy_movement, which also carries the uploaded CODECO
# batches, whereas KPI 3 must be computed from the corpus gate log alone. Both
# are read-only and neither touches the other's table.
#
#   GET /api/cfs-ecy/events                        -> the raw gate events
#   GET /api/cfs-ecy/empty-trt                     -> the KPI + its provenance
#   GET /api/cfs-ecy/empty-trt/chains              -> per-container lifecycles
#   GET /api/cfs-ecy/empty-trt/anomalies/{code}    -> containers behind a finding
#   GET /api/cfs-ecy/empty-trt/containers/{cn}     -> one container end to end
_trt_service: Optional[EmptyTrtService] = None


def get_trt_service(request: Request) -> EmptyTrtService:
    global _trt_service
    if _trt_service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _trt_service = EmptyTrtService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _trt_service


def _location_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in ("CFS", "ECY"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_location_type",
                                    "location_type": value,
                                    "allowed": ["CFS", "ECY"]})
    return v


def _event_type(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in _CODECO_EVENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_event_type", "event_type": value,
                                    "allowed": list(_CODECO_EVENT_TYPES)})
    return v


@router.get("/events", response_model=Page,
            summary="CFS/ECY CODECO gate events (core.container_event)")
async def list_gate_events(
    response: Response,
    container: Optional[str] = Query(default=None, description="container number contains"),
    location_type: Optional[str] = Query(default=None, description="CFS | ECY"),
    event_type: Optional[str] = Query(default=None,
                                      description="ECY_OUT | ECY_IN | CFS_IN | CFS_OUT"),
    direction: Optional[str] = Query(default=None, description="I | O"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    sort: str = Query(default="event_ts"),
    order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    svc: EmptyTrtService = Depends(get_trt_service),
) -> Page:
    filters = {"container": container,
               "location_type": _location_type(location_type),
               "event_type": _event_type(event_type),
               "direction": direction,
               "ts_from": date_from, "ts_to": date_to}
    res = await svc.list_events(filters, sort=sort, direction=order,
                                limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/empty-trt",
            summary="KPI 3 — TRT for empty containers from ECD (real CODECO data)")
async def empty_trt(svc: EmptyTrtService = Depends(get_trt_service)) -> Dict[str, Any]:
    """The KPI result plus the evidence behind it.

    ``kpi`` is the standard KpiResult envelope (value / target 45 min / baseline
    72 min / deltaPct / onTarget / source / n). ``source`` reports the imported
    event inventory the ECY 529-OUT-vs-432-IN gap is read off, ``anomalies`` and
    ``data_quality`` report what was excluded and why.
    """
    res = await svc.kpi()
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res


@router.get("/empty-trt/chains", response_model=Page,
            summary="Per-container empty lifecycles (ECY-Out → CFS-In → CFS-Out)")
async def empty_trt_chains(
    response: Response,
    container: Optional[str] = Query(default=None),
    chain_status: Optional[str] = Query(default=None,
                                        description="COMPLETE | PARTIAL | ORPHAN"),
    anomaly_code: Optional[str] = Query(default=None),
    anomaly_only: bool = Query(default=False),
    sort: str = Query(default="ecy_out_ts"),
    order: str = Query(default="asc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    svc: EmptyTrtService = Depends(get_trt_service),
) -> Page:
    if chain_status and chain_status.strip().upper() not in ("COMPLETE", "PARTIAL", "ORPHAN"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_chain_status",
                                    "chain_status": chain_status})
    filters = {"container": container, "chain_status": chain_status,
               "anomaly_code": anomaly_code, "anomaly_only": anomaly_only}
    res = await svc.list_chains(filters, sort=sort, direction=order,
                                limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return _page(res["items"], res["total"], limit, offset, response)


@router.get("/empty-trt/anomalies/{code}",
            summary="The containers behind one anomaly code (detected, not patched)")
async def empty_trt_anomaly(code: str,
                            limit: int = Query(default=100, ge=1, le=1000),
                            offset: int = Query(default=0, ge=0),
                            svc: EmptyTrtService = Depends(get_trt_service)) -> Dict[str, Any]:
    res = await svc.anomaly_containers(code, limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res


@router.get("/empty-trt/containers/{container_no}",
            summary="One container's empty lifecycle: legs, durations, raw events")
async def empty_trt_container(container_no: str,
                              svc: EmptyTrtService = Depends(get_trt_service)) -> Dict[str, Any]:
    res = await svc.container(container_no)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "container_not_found",
                                    "container_no": container_no.strip().upper()})
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return res
