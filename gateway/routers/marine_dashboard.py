"""/api/marine dashboard reads — UC-I operational boards (additive).

Hosts the UC-1 PoC dashboard family consumed by ``jnpa_poc_1``:

    GET /api/marine/berthing-plan      5-day plan (UI-028 / UC1-024)
    GET /api/marine/vessel-states      ledger traffic map (UI-020 / UC1-011)
    GET /api/marine/berths             berth register + occupancy (M-04)
    GET /api/marine/kpis               tender KPI cards (M-09 / UI-041)
    GET /api/marine/arrivals-departures bucketed counts (M-10)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.berthing_plan import BerthingPlanService
from services.marine.dashboard_boards import DashboardBoardsService

router = APIRouter(prefix="/api/marine", tags=["marine"])

_API = "marine_dashboard"
_plan_svc: Optional[BerthingPlanService] = None
_boards_svc: Optional[DashboardBoardsService] = None


def get_plan_service(request: Request) -> BerthingPlanService:
    global _plan_svc
    if _plan_svc is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _plan_svc = BerthingPlanService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _plan_svc


def get_boards_service(request: Request) -> DashboardBoardsService:
    global _boards_svc
    if _boards_svc is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _boards_svc = DashboardBoardsService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _boards_svc


# ------------------------------------------------------------------ shared envelope
class EnvelopeOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data_mode: str
    source: str
    observed_at: Optional[datetime] = None
    as_of: Optional[datetime] = None


# ------------------------------------------------------------------ berthing plan (existing)
class PlanWindowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: datetime
    end: datetime
    anchor: datetime


class PlanEntryOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    source: str
    berth_code: str = ""
    berth_raw: str = ""
    terminal: str = ""
    vessel_name: str = ""
    voyage_no: str = ""
    imo_no: str = ""
    shipping_line: str = ""
    status: str = ""
    start_ts: datetime
    end_ts: datetime
    end_estimated: bool = False
    ref: str = ""
    vcn: str = ""
    via_no: str = ""


class BerthingPlanOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data_mode: str
    source: str
    observed_at: Optional[datetime] = None
    as_of: Optional[datetime] = None
    window: PlanWindowOut
    entries: List[PlanEntryOut]


@router.get("/berthing-plan", response_model=BerthingPlanOut,
            summary="5-day berthing plan — confirmed (reports) vs indicative (twin)")
async def berthing_plan(
    days: int = Query(default=5, ge=1, le=14,
                      description="Forward horizon in days (tender minimum = 5)"),
    at: Optional[datetime] = Query(
        default=None,
        description="Sim/demo pin (ISO). Omitted → latest berthing actual in corpus.",
    ),
    service: BerthingPlanService = Depends(get_plan_service),
) -> BerthingPlanOut:
    res: dict[str, Any] = await service.plan(at=at, days=days)
    REQUESTS.labels(_API, "ok").inc()
    return BerthingPlanOut(**res)


# ------------------------------------------------------------------ vessel states (UC1-011)
class VesselStateItemOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_id: Optional[int] = None
    vcn: str = ""
    via_no: str = ""
    imo_no: str = ""
    vessel_name: str = ""
    voyage_no: str = ""
    status: str = ""
    state: str = ""
    berth_code: str = ""
    terminal: str = ""
    eta: Optional[datetime] = None
    etb: Optional[datetime] = None
    etd: Optional[datetime] = None
    ata: Optional[datetime] = None
    atd: Optional[datetime] = None
    anchor_down_at: Optional[datetime] = None
    pilot_boarded_at: Optional[datetime] = None
    first_line_at: Optional[datetime] = None
    movement_type: str = ""


class VesselStatesOut(EnvelopeOut):
    items: List[VesselStateItemOut]


@router.get("/vessel-states", response_model=VesselStatesOut,
            summary="Ledger-derived vessel states for the traffic map (UC1-011)")
async def vessel_states(
    at: Optional[datetime] = Query(
        default=None,
        description="Sim/demo pin (ISO). Positions are synthesised client-side.",
    ),
    service: DashboardBoardsService = Depends(get_boards_service),
) -> VesselStatesOut:
    res = await service.vessel_states(at=at)
    REQUESTS.labels(_API, "ok").inc()
    return VesselStatesOut(**res)


# ------------------------------------------------------------------ berths
class BerthBoardItemOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    berth_id: int
    code: str = ""
    terminal: str = ""
    terminal_name: str = ""
    operator: str = ""
    length_m: Optional[float] = None
    design_depth_m: Optional[float] = None
    dimensions_assumed: bool = True
    state: str = "free"
    vessel_name: str = ""
    voyage_no: str = ""
    imo_no: str = ""
    shipping_line: str = ""
    alongside_since: Optional[datetime] = None
    ops_start: Optional[datetime] = None
    ops_end: Optional[datetime] = None
    record_status: str = ""


class BerthsBoardOut(EnvelopeOut):
    items: List[BerthBoardItemOut]
    occupied: int = 0


@router.get("/berths", response_model=BerthsBoardOut,
            summary="Berth register + occupancy at the anchor instant")
async def berths_board(
    at: Optional[datetime] = Query(default=None, description="Sim/demo pin (ISO)"),
    service: DashboardBoardsService = Depends(get_boards_service),
) -> BerthsBoardOut:
    res = await service.berths(at=at)
    REQUESTS.labels(_API, "ok").inc()
    return BerthsBoardOut(**res)


# ------------------------------------------------------------------ KPIs
class KpiSeriesPointOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    t: datetime
    v: float


class KpiBaselineOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    value: Optional[float] = None
    unit: str = ""
    period: str = ""
    previous_value: Optional[float] = None
    previous_period: str = ""
    source_document: str = ""
    source_url: str = ""
    notes: str = ""


class KpiCardOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    key: str
    name: str
    value: Optional[float] = None
    median: Optional[float] = None
    unit: str = ""
    n: int = 0
    definition: str = ""
    basis: str = ""
    baseline_source: str = ""
    baseline: Optional[KpiBaselineOut] = None
    vs_baseline_pct: Optional[float] = None
    note: str = ""
    series: List[KpiSeriesPointOut] = []


class KpiWindowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    days: int
    anchor: datetime


class KpisBoardOut(EnvelopeOut):
    window: KpiWindowOut
    kpis: List[KpiCardOut]


@router.get("/kpis", response_model=KpisBoardOut,
            summary="Tender KPI cards with definitions (M-09 / UI-041)")
async def kpis_board(
    at: Optional[datetime] = Query(default=None, description="Sim/demo pin (ISO)"),
    window_days: int = Query(default=30, ge=1, le=90, alias="window_days"),
    service: DashboardBoardsService = Depends(get_boards_service),
) -> KpisBoardOut:
    res = await service.kpis(at=at, window_days=window_days)
    REQUESTS.labels(_API, "ok").inc()
    return KpisBoardOut(**res)


# ------------------------------------------------------------------ arrivals / departures
class ArrDepBlockOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    bucket_start: datetime
    arrivals: int = 0
    departures: int = 0


class ArrDepOut(EnvelopeOut):
    bucket_hours: int = 4
    blocks: List[ArrDepBlockOut]


@router.get("/arrivals-departures", response_model=ArrDepOut,
            summary="Bucketed arrival/departure counts around the anchor instant")
async def arrivals_departures(
    at: Optional[datetime] = Query(default=None, description="Sim/demo pin (ISO)"),
    hours: int = Query(default=48, ge=4, le=336),
    bucket_hours: int = Query(default=4, ge=1, le=24, alias="bucket_hours"),
    service: DashboardBoardsService = Depends(get_boards_service),
) -> ArrDepOut:
    res = await service.arrivals_departures(at=at, hours=hours, bucket_hours=bucket_hours)
    REQUESTS.labels(_API, "ok").inc()
    return ArrDepOut(**res)
