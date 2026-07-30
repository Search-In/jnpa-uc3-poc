"""/api/marine/calls — UC-I Marine vessel-call spine (read-only, additive).

A thin router over :class:`services.marine.VesselCallService`, in the same mould as
gateway/routers/berthing.py. It serves the canonical vessel-visit model backing Use
Case I (Vessel Traffic Management & Optimisation): one row per call, its ordered
actuals, and the turnaround / pre-berthing-delay aggregates.

    GET /api/marine/calls                    -> list + filter/search/paginate calls
    GET /api/marine/calls/stats              -> UC-I KPI aggregates + distributions
    GET /api/marine/calls/by-vcn/{vcn}       -> resolve a full PCS VCN (single call)
    GET /api/marine/calls/by-via/{via_no}    -> resolve a short VIA (MAY be several)
    GET /api/marine/calls/{call_id}          -> one vessel call
    GET /api/marine/calls/{call_id}/timeline -> one call + its ordered actuals
    GET /api/marine/calls/{call_id}/events   -> the actuals alone, paginated

Reads ONLY core.vessel_call / core.vessel_call_event. It never touches the jnpa
schema, so jnpa.berthing_* and every other UC3 module are entirely unaffected — a
berthing response contract cannot change as a result of this router existing.

Read-only slice: no write endpoints, hence no upload write-gate (compare
berthing.require_uploader). Ingestion arrives in a later slice.

RBAC: /api/marine has no explicit gateway/auth.py policy entry yet, so it currently
falls through to the default "any authenticated stakeholder" rule. A dedicated
("/api/marine", CONTROL_ROOM | {MARINE_OPS, ...}) entry is a follow-up.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine import VesselCallService

router = APIRouter(prefix="/api/marine/calls", tags=["marine"])

_API = "marine_calls"

_service: Optional[VesselCallService] = None


def get_service(request: Request) -> VesselCallService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = VesselCallService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- DTOs
class CallOut(BaseModel):
    """core.vessel_call, in migration 0038 declaration order.

    Every field is Optional because a call is created on first sighting and enriched
    in place: a CALINF-seeded call carries neither VCN nor terminal yet.
    """
    model_config = ConfigDict(extra="ignore")
    call_id: Optional[int] = None
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_no: Optional[str] = None
    rotation_no: Optional[str] = None
    terminal_id: Optional[int] = None
    berth_id: Optional[int] = None
    purpose: Optional[str] = None
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    etb: Optional[datetime] = None
    ata: Optional[datetime] = None
    atd: Optional[datetime] = None
    atc: Optional[datetime] = None
    status: Optional[str] = None
    igm_no: Optional[int] = None
    source_note: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class CallListResponse(BaseModel):
    items: List[CallOut]
    total: int
    limit: int
    offset: int
    count: int


class EventOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    event_id: Optional[int] = None
    call_id: Optional[int] = None
    event_type: Optional[str] = None
    event_ts: Optional[datetime] = None
    berth_id: Optional[int] = None
    source_file: Optional[int] = None
    created_at: Optional[datetime] = None


class EventListResponse(BaseModel):
    call_id: int
    items: List[EventOut]
    limit: int
    offset: int
    count: int


class TimelineOut(CallOut):
    """One call plus its actuals, ordered by event_ts (migration 0038 [D6])."""
    events: List[EventOut] = []


class ViaLookupResponse(BaseModel):
    """A short VIA recycles across years (migration 0038 [D10]) -> 0..n calls."""
    via_no: str
    items: List[CallOut]
    count: int


class StatusStat(BaseModel):
    status: Optional[str] = None
    count: int


class TerminalStat(BaseModel):
    terminal_id: Optional[int] = None
    count: int
    in_port: int


class StatsOut(BaseModel):
    total: int
    with_vcn: int
    without_vcn: int
    arrived: int
    in_port: int
    ops_completed: int
    departed: int
    terminals: int
    avg_turnaround_hours: Optional[float] = None
    avg_pre_berth_delay_hours: Optional[float] = None
    by_status: List[StatusStat]
    by_terminal: List[TerminalStat]


# ------------------------------------------------------------------- helpers
def _filters(vcn, via, imo, vessel, voyage, rotation, terminal_id, berth_id,
             status_, has_vcn, in_port, eta_from, eta_to) -> Dict[str, Any]:
    """Assemble the repository filter map.

    No vocabulary validation: migration 0038 [D8] leaves status free-text and
    terminal_id is a numeric FK column whose parent (core.ref_terminal) does not exist
    yet, so there is nothing to validate against. Every value is bound as a parameter
    by the repository.
    """
    return {"vcn": vcn, "via": via, "imo_no": imo, "vessel": vessel, "voyage": voyage,
            "rotation": rotation, "terminal_id": terminal_id, "berth_id": berth_id,
            "status": status_, "has_vcn": has_vcn, "in_port": bool(in_port),
            "eta_from": eta_from, "eta_to": eta_to}


def _not_found(error: str, **detail: Any) -> HTTPException:
    REQUESTS.labels(_API, "not_found").inc()
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                         detail={"error": error, **detail})


# ------------------------------------------------------------------- read endpoints
@router.get("", response_model=CallListResponse,
            summary="List / search UC-I vessel calls")
async def list_calls(
    vcn: Optional[str] = Query(default=None, description="exact full PCS VCN"),
    via: Optional[str] = Query(default=None, description="short VIA contains"),
    imo: Optional[str] = Query(default=None, alias="imo_no", description="exact IMO number"),
    vessel: Optional[str] = Query(default=None, description="vessel name contains"),
    voyage: Optional[str] = Query(default=None, description="voyage no contains"),
    rotation: Optional[str] = Query(default=None, description="rotation no contains"),
    terminal_id: Optional[int] = Query(default=None),
    berth_id: Optional[int] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status"),
    has_vcn: Optional[bool] = Query(default=None,
                                    description="true = VCN assigned, false = still pre-VCN"),
    in_port: bool = Query(default=False, description="arrived but not yet sailed"),
    date_from: Optional[datetime] = Query(default=None, alias="from", description="ETA >="),
    date_to: Optional[datetime] = Query(default=None, alias="to", description="ETA <="),
    sort: str = Query(default="updated_at"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: VesselCallService = Depends(get_service),
) -> CallListResponse:
    filters = _filters(vcn, via, imo, vessel, voyage, rotation, terminal_id, berth_id,
                       status_, has_vcn, in_port, date_from, date_to)
    res = await service.list_calls(filters, sort=sort, direction=direction,
                                   limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return CallListResponse(**res)


@router.get("/stats", response_model=StatsOut,
            summary="UC-I KPI aggregates (turnaround, pre-berthing delay) + distributions")
async def stats(
    terminal_id: Optional[int] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status"),
    has_vcn: Optional[bool] = Query(default=None),
    in_port: bool = Query(default=False),
    date_from: Optional[datetime] = Query(default=None, alias="from", description="ETA >="),
    date_to: Optional[datetime] = Query(default=None, alias="to", description="ETA <="),
    service: VesselCallService = Depends(get_service),
) -> StatsOut:
    filters = _filters(None, None, None, None, None, None, terminal_id, None,
                       status_, has_vcn, in_port, date_from, date_to)
    res = await service.stats(filters)
    REQUESTS.labels(_API, "ok").inc()
    return StatsOut(**res)


@router.get("/by-vcn/{vcn}", response_model=CallOut,
            summary="Resolve a full PCS VCN to its vessel call")
async def get_by_vcn(vcn: str,
                     service: VesselCallService = Depends(get_service)) -> CallOut:
    res = await service.get_by_vcn(vcn)
    if res is None:
        raise _not_found("vessel_call_not_found", vcn=vcn)
    REQUESTS.labels(_API, "ok").inc()
    return CallOut(**res)


@router.get("/by-via/{via_no}", response_model=ViaLookupResponse,
            summary="Resolve a short VIA number — may match several calls")
async def get_by_via(via_no: str,
                     service: VesselCallService = Depends(get_service)) -> ViaLookupResponse:
    items = await service.get_by_via(via_no)
    if not items:
        raise _not_found("vessel_call_not_found", via_no=via_no)
    REQUESTS.labels(_API, "ok").inc()
    return ViaLookupResponse(via_no=via_no, items=[CallOut(**i) for i in items],
                             count=len(items))


# ------------------------------------------------------------------- one call (declared last so
# the static /stats, /by-vcn and /by-via prefixes win)
@router.get("/{call_id}", response_model=CallOut, summary="One UC-I vessel call")
async def get_call(call_id: int,
                   service: VesselCallService = Depends(get_service)) -> CallOut:
    res = await service.get(call_id)
    if res is None:
        raise _not_found("vessel_call_not_found", call_id=call_id)
    REQUESTS.labels(_API, "ok").inc()
    return CallOut(**res)


@router.get("/{call_id}/timeline", response_model=TimelineOut,
            summary="One vessel call + its ordered actuals")
async def get_timeline(call_id: int,
                       service: VesselCallService = Depends(get_service)) -> TimelineOut:
    res = await service.timeline(call_id)
    if res is None:
        raise _not_found("vessel_call_not_found", call_id=call_id)
    REQUESTS.labels(_API, "ok").inc()
    return TimelineOut(**res)


@router.get("/{call_id}/events", response_model=EventListResponse,
            summary="Actuals for one vessel call (anchored / pilot boarded / all fast / ...)")
async def list_events(
    call_id: int,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: VesselCallService = Depends(get_service),
) -> EventListResponse:
    # 404 on an unknown call rather than returning an empty event list, so the caller
    # can distinguish "no such call" from "call exists but has no actuals yet".
    if await service.get(call_id) is None:
        raise _not_found("vessel_call_not_found", call_id=call_id)
    items = await service.list_events(call_id, limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return EventListResponse(call_id=call_id, items=[EventOut(**i) for i in items],
                             limit=limit, offset=offset, count=len(items))
