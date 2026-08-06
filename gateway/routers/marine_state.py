"""/api/marine/state — UC-I business state derived from the marine lifecycle (read-only).

A NEW router with NEW response models. Nothing existing is touched: marine_calls.py, its
CallOut / EventOut / StatsOut, every other marine router and the whole repository layer are
unchanged, so no client that does not call these paths can observe a difference.

    GET /api/marine/state/calls/{call_id}   -> business state of one call
    GET /api/marine/state/berths            -> berth occupancy derived from the lifecycle

The single-call endpoint stays for consumers that hold only a call id and want the state
without the events payload. The Vessel Timeline pane no longer uses it: /api/marine/calls/
{id}/timeline now carries the same projection inline, so that pane makes one request. Both
answers come from the same projection, so they cannot drift.

Every value is produced by services.marine.state_engine. This router contains no lifecycle
rule of its own — no ordering, no status ladder, no occupancy predicate.

RBAC: covered by the existing ("/api/marine", CONTROL_ROOM | CUSTOMS) policy in
gateway/auth.py — no new policy entry required.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.state_service import MarineStateService

router = APIRouter(prefix="/api/marine/state", tags=["marine"])

_API = "marine_state"
_service: Optional[MarineStateService] = None


def get_service(request: Request) -> MarineStateService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = MarineStateService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class CallStateOut(BaseModel):
    """Business state of one vessel call. Mirrors state_engine.CallState plus identity."""
    model_config = ConfigDict(extra="ignore")
    call_id: int
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    event_count: int = 0
    #: Operational stage — the parser stage until milestones exist, then engine-derived.
    status: Optional[str] = None
    arrival_state: str
    berth_state: str
    pilot_state: str
    #: ADDITIVE, optional. Craft currently committed to this call — 'Committed' while any
    #: commitment is live and unreleased, else 'Idle'. Distinct from `portcraft_state`,
    #: which is the engine's verdict on whether the movement REQUIRES craft: demand versus
    #: supply, deliberately not conflated.
    craft_state: Optional[str] = None
    #: ADDITIVE, optional. How many craft are committed right now.
    craft_committed: Optional[int] = None

    departure_state: str
    shipping_state: str
    portcraft_state: str
    is_in_port: bool
    is_at_berth: bool
    #: Highest-RANK milestone reached, not the latest by clock — the corpus ties
    #: ARRIVED with BERTHED and puts SAILED before DEPARTED.
    latest_event: Optional[str] = None
    latest_event_time: Optional[_dt.datetime] = None


class BerthCallOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_id: int
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    berth_state: str
    is_at_berth: bool
    latest_event: Optional[str] = None


class BerthOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    berth_id: int
    code: Optional[str] = None
    terminal_id: Optional[int] = None
    #: Occupied | Allotted | Free — derived by the engine from each call's milestones.
    state: str
    occupied_by: Optional[BerthCallOut] = None
    inbound: List[BerthCallOut] = []


class BerthOccupancyOut(BaseModel):
    berths: List[BerthOut]
    total: int
    occupied: int
    allotted: int
    free: int


class KpiScopeOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    active_calls: int
    #: Always 'projection' — states this endpoint derives nothing of its own.
    basis: str


class PilotKpiOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    busy: int
    available: int
    known: int
    utilisation_pct: float
    #: Calls whose pilot job is not yet finished.
    demand: int
    #: Calls with no pilot assigned at all.
    waiting_assignment: int
    under_pilotage: int
    completed: int


class CraftKpiOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    busy: int
    available: int
    fleet_total: int
    utilisation_pct: float
    #: The engine's verdict that a movement requires craft.
    demand: int
    committed_calls: int
    #: Requires craft, none committed.
    waiting_assignment: int
    #: Busy by the engine's verdict but in no reportable phase — reconciles `demand`
    #: with the Port Craft board's total.
    demand_unphased: int


class OperationsKpiOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    marine_support_required: int
    awaiting_berthing: int
    at_berth: int
    under_pilotage: int
    preparing_departure: int
    sailing: int
    #: The one KPI that reaches outside the active set, by definition.
    completed_today: int


class MarineKpisOut(BaseModel):
    """Operational KPIs, every one a tally of the Marine Projection's own verdicts.

    ADDITIVE. `/api/marine/calls/stats` is untouched and keeps reporting the factual
    column aggregates it always did; this endpoint answers the different question of what
    is happening operationally right now.
    """
    model_config = ConfigDict(extra="ignore")
    scope: KpiScopeOut
    pilot: PilotKpiOut
    craft: CraftKpiOut
    operations: OperationsKpiOut


@router.get("/calls/{call_id}", response_model=CallStateOut,
            summary="Business state of one vessel call (Vessel Timeline)")
async def call_state(call_id: int,
                     service: MarineStateService = Depends(get_service)) -> CallStateOut:
    res: Optional[Dict[str, Any]] = await service.call_state(call_id)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "vessel_call_not_found", "call_id": call_id})
    REQUESTS.labels(_API, "ok").inc()
    return CallStateOut(**res)


@router.get("/berths", response_model=BerthOccupancyOut,
            summary="Berth occupancy derived from the vessel lifecycle")
async def berth_occupancy(
        service: MarineStateService = Depends(get_service)) -> BerthOccupancyOut:
    res = await service.berth_occupancy()
    REQUESTS.labels(_API, "ok").inc()
    return BerthOccupancyOut(**res)


class CraftTypeCount(BaseModel):
    craft_type: Optional[str] = None
    count: int


class FleetOut(BaseModel):
    total: int
    by_type: List[CraftTypeCount]


class CraftDemandCounts(BaseModel):
    total: int
    inbound_movement: int
    alongside: int
    outbound_movement: int


class CraftMovementOut(BaseModel):
    """One CALL requiring craft, described by its own lifecycle.

    The lifecycle fields below are ADDITIVE and optional: they are copied verbatim from
    the CallProjection this row was already built from, so a consumer can see WHY the call
    counts toward demand rather than only that it does. A client that ignores them is
    unaffected.

    There is deliberately NO craft identity and no requires_* flag — nothing in the schema
    links a craft to a call, so either would be invented.
    """
    model_config = ConfigDict(extra="ignore")
    call_id: int
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    berth_id: Optional[int] = None
    latest_event: Optional[str] = None
    # --- additive lifecycle, straight from the projection ---
    imo_no: Optional[str] = None
    status: Optional[str] = None
    arrival_state: Optional[str] = None
    pilot_state: Optional[str] = None
    berth_state: Optional[str] = None
    departure_state: Optional[str] = None
    shipping_state: Optional[str] = None
    portcraft_state: Optional[str] = None
    latest_event_time: Optional[_dt.datetime] = None
    #: Which bucket this row landed in — Inbound | Alongside | Outbound. Names the phase
    #: the service already decided; it is not a second classification.
    movement_phase: Optional[str] = None


class PortCraftDemandOut(BaseModel):
    """Craft demand from the lifecycle, against the real fleet register.

    NO per-craft assignment and NO utilisation percentage: core.port_craft holds no
    operational state and nothing links a craft to a call, so which tug is on which job —
    and therefore any utilisation ratio — is not in the data.
    """
    fleet: FleetOut
    demand: CraftDemandCounts
    inbound_movement: List[CraftMovementOut]
    alongside: List[CraftMovementOut]
    outbound_movement: List[CraftMovementOut]
    active_calls: int


@router.get("/port-craft", response_model=PortCraftDemandOut,
            summary="Port-craft demand derived from the vessel lifecycle")
async def port_craft_demand(
        service: MarineStateService = Depends(get_service)) -> PortCraftDemandOut:
    res = await service.port_craft_demand()
    REQUESTS.labels(_API, "ok").inc()
    return PortCraftDemandOut(**res)


class SlLifecycleOut(BaseModel):
    """Lifecycle of the call a shipping-line visit resolved to.

    No `current_position`: neither core.vessel_call nor core.vessel_call_event carries a
    coordinate, so a position would have to be invented. Live position is the AIS layer's.
    """
    model_config = ConfigDict(extra="ignore")
    call_id: int
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    status: Optional[str] = None
    arrival_state: Optional[str] = None
    berth_state: Optional[str] = None
    departure_state: Optional[str] = None
    shipping_state: Optional[str] = None
    is_in_port: bool = False
    is_at_berth: bool = False
    berth_id: Optional[int] = None
    eta: Optional[_dt.datetime] = None
    etd: Optional[_dt.datetime] = None
    arrived_at: Optional[_dt.datetime] = None
    berthed_at: Optional[_dt.datetime] = None
    departed_at: Optional[_dt.datetime] = None
    latest_event: Optional[str] = None


class SlVisitOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    shipping_line: Optional[str] = None
    vessel_visit: Optional[str] = None
    voyage: Optional[str] = None
    containers: int = 0
    #: 'exact' | 'composite' (3-char code stripped, doc 01 §1.9) | null when unresolved.
    match: Optional[str] = None
    lifecycle: Optional[SlLifecycleOut] = None


class SlLineRollupOut(BaseModel):
    shipping_line: Optional[str] = None
    visits: int = 0
    active: int = 0
    historical: int = 0
    unmatched: int = 0
    containers: int = 0


class ShippingLineProgressOut(BaseModel):
    items: List[SlVisitOut]
    count: int
    matched: int
    unmatched: int
    matched_exact: int
    matched_composite: int
    by_line: List[SlLineRollupOut]


@router.get("/shipping-lines", response_model=ShippingLineProgressOut,
            summary="Shipping-line vessel progress derived from the lifecycle")
async def shipping_line_progress(
    line: Optional[str] = Query(default=None, description="filter to one line code"),
    limit: int = Query(default=500, ge=1, le=2000),
    service: MarineStateService = Depends(get_service),
) -> ShippingLineProgressOut:
    res = await service.shipping_line_progress(line=line, limit=limit)
    REQUESTS.labels(_API, "ok").inc()
    return ShippingLineProgressOut(**res)


class BerthingLifecycleOut(CallStateOut):
    """The engine state for the call a berthing report resolved to."""


class BerthingReconciledOut(BaseModel):
    """One berthing-report row with its PCS lifecycle state ALONGSIDE, never merged."""
    model_config = ConfigDict(extra="ignore")
    record_id: int
    terminal: Optional[str] = None
    vessel_name: Optional[str] = None
    voyage_number: Optional[str] = None
    berth_number: Optional[str] = None
    #: The PDF-sourced status, verbatim. Its vocabulary (EXPECTED..DEPARTED) is CHECK-
    #: constrained on core.berthing_record and is NOT the engine's vocabulary.
    report_status: Optional[str] = None
    eta: Optional[_dt.datetime] = None
    ata: Optional[_dt.datetime] = None
    berthing_time: Optional[_dt.datetime] = None
    departure_time: Optional[_dt.datetime] = None
    #: None when the VIA resolves to no call — a real finding, not an error.
    lifecycle: Optional[BerthingLifecycleOut] = None


class BerthingReconciledPage(BaseModel):
    items: List[BerthingReconciledOut]
    count: int
    matched: int
    unmatched: int
    limit: int
    offset: int


@router.get("/berthing", response_model=BerthingReconciledPage,
            summary="Berthing reports reconciled against the PCS call lifecycle")
async def berthing_reconciliation(
    terminal: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: MarineStateService = Depends(get_service),
) -> BerthingReconciledPage:
    res = await service.berthing_reconciliation(terminal=terminal, limit=limit,
                                                offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return BerthingReconciledPage(**res)


@router.get("/kpis", response_model=MarineKpisOut,
            summary="Operational KPIs derived from the Marine Projection")
async def kpis(service: MarineStateService = Depends(get_service)) -> dict:
    """Read-only. Adds no rule of its own — see MarineStateService.kpis."""
    return await service.kpis()
