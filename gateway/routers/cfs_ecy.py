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
from ..metrics import REQUESTS
from services.cfs_ecy import CfsEcyService, CfsEcyUploadService

router = APIRouter(prefix="/api/cfs-ecy", tags=["cfs-ecy"])

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB, mirrors the shipping-lines upload cap
# Roles allowed to upload: control room + customs (+ admin ⊂ control room).
_UPLOADER_ROLES = CONTROL_ROOM | {Role.CUSTOMS.value}

_service: Optional[CfsEcyService] = None
_upload_service: Optional[CfsEcyUploadService] = None


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


def _filters(facility, mode, container, date_from, date_to) -> Dict[str, Any]:
    return {
        "facility_type": _facility(facility),
        "mode": _mode(mode),
        "container": container,
        "ts_from": date_from,
        "ts_to": date_to,
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
    service: CfsEcyService = Depends(get_service),
) -> MovementListResponse:
    filters = _filters(facility, mode, container, date_from, date_to)
    res = await service.list_movements(filters, sort=sort, direction=direction,
                                       limit=limit, offset=offset)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return MovementListResponse(**res)


@router.get("/stats", response_model=StatsOut, summary="CFS-ECY KPI aggregates + daily throughput")
async def stats(
    facility: Optional[str] = Query(default=None, description="CFS | ECY"),
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    service: CfsEcyService = Depends(get_service),
) -> StatsOut:
    filters = _filters(facility, None, None, date_from, date_to)
    res = await service.stats(filters)
    REQUESTS.labels("cfs_ecy", "ok").inc()
    return StatsOut(**res)


@router.get("/dwell", response_model=DwellResponse, summary="CFS dwell report (OUT - IN)")
async def dwell(
    date_from: Optional[datetime] = Query(default=None, alias="from"),
    date_to: Optional[datetime] = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: CfsEcyService = Depends(get_service),
) -> DwellResponse:
    filters = {"ts_from": date_from, "ts_to": date_to}
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
