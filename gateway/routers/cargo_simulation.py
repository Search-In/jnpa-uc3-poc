"""/api/cargo/simulate + /api/gate/hourly-profile — the UC-3 what-if surface.

A thin router in the same mould as :mod:`gateway.routers.cargo`: validate with
Pydantic v2 DTOs, delegate every calculation to
:class:`services.cargo.simulation.SimulationService`, map the typed error to a
status code. No SQL and no arithmetic here.

    GET  /api/cargo/simulate/scenarios          -> the catalog
    POST /api/cargo/simulate/berth-cascade      -> I-B   extended berth window
    POST /api/cargo/simulate/crane-productivity -> II-B  equipment availability
    POST /api/cargo/simulate/modal-shift        -> II-A  rail -> road
    POST /api/cargo/simulate/gate-slotting      -> III-A gate congestion
    POST /api/cargo/simulate/driver-shortage    -> III-B driver shortage
    POST /api/cargo/simulate/{scenario}         -> generic passthrough
    GET  /api/gate/hourly-profile               -> hourly arrivals over any window

Every response carries the JNPA Notice §1 contract: ``method``, ``result`` +
``figures``, ``assumptions`` (separately from the result), ``queries`` (the SQL
and bound parameters, so the working can be traced) and ``recommendations``.

ROUTE ORDERING / RBAC
---------------------
The paths sit under ``/api/cargo/simulate/...`` (two segments after the prefix),
so they cannot be captured by ``GET /api/cargo/{container_number}`` in the cargo
router, which matches a single segment. This router is nonetheless registered
BEFORE ``cargo.router`` in gateway/main.py so the ordering is explicit rather than
incidental.

No new RBAC rule is needed and none is added: ``/api/cargo`` writes are already
restricted to control-room + customs by the method overlay in gateway/auth.py, and
``/api/gate/`` is already control-room + customs. The simulate endpoints inherit
both. They are POSTs because the parameter set is a body, not because they mutate
anything — the simulation layer is read-only by construction.

NO ``response_model``: each scenario returns its own result shape (a berth cascade
and a driver shortage share only the envelope), so the DTOs validate the REQUEST
and the response is the service's already-JSON-safe dict.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.cargo.simulation import SimulationError, SimulationService

from ..logging import get_logger
from ..metrics import REQUESTS

log = get_logger("gateway.cargo_simulation")

# Full paths rather than a prefix: this router owns two unrelated path families
# (/api/cargo/simulate and /api/gate/hourly-profile). Same pattern as
# gateway/routers/container_job.py.
router = APIRouter(tags=["cargo-simulation"])

_MAX_WINDOW_DAYS = 92


# --------------------------------------------------------------------------- deps
_service: Optional[SimulationService] = None


def get_service(request: Request) -> SimulationService:
    """Module-singleton service bound to the gateway DSN, dependency-injected so
    tests can override it with a fake-repo-backed instance."""
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = SimulationService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


def _utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC. The simulation compares request bounds with
    timestamptz columns, and mixing naive and aware datetimes raises."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _day_start(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


# ---------------------------------------------------------------------- request DTOs
class VesselBunchingIn(BaseModel):
    """Scenario I-A.

    ``objective`` is required by the Notice to be *stated*, so it is an explicit
    input rather than a backend default with an opinion baked into it. Every
    candidate ordering is then scored against whichever objective was chosen, so
    the alternatives are comparable by construction."""
    as_of: datetime = Field(..., description="The study day (ISO-8601)")
    terminal: Optional[str] = Field(default=None, max_length=64)
    horizon_hours: int = Field(default=24, ge=1, le=336)
    objective: Literal["waiting_time", "moves_handled", "line_priority"] = Field(
        default="waiting_time",
        description="The basis the proposed order optimises for")

    model_config = ConfigDict(json_schema_extra={"example": {
        "as_of": "2026-08-06T00:00:00Z", "objective": "waiting_time",
        "horizon_hours": 24}})


class BerthCascadeIn(BaseModel):
    """Scenario I-B. ``as_of`` opens the horizon; the overrun is applied to the
    named call, or to the first call in the window when none is named."""
    terminal: Optional[str] = Field(default=None, max_length=64)
    as_of: datetime = Field(..., description="Start of the cascade horizon (ISO-8601)")
    delay_hours: float = Field(default=6.0, gt=0, le=240,
                               description="The operation overrun, in hours")
    horizon_hours: int = Field(default=48, ge=1, le=336)
    vessel_name: Optional[str] = Field(default=None, max_length=200)
    voyage_number: Optional[str] = Field(default=None, max_length=64)
    berthing_record_id: Optional[int] = Field(default=None, ge=1)

    model_config = ConfigDict(json_schema_extra={"example": {
        "terminal": "NSICT", "as_of": "2026-08-02T00:00:00Z",
        "delay_hours": 6, "horizon_hours": 48}})


class CraneProductivityIn(BaseModel):
    """Scenario II-B."""
    terminal: Optional[str] = Field(default=None, max_length=64)
    as_of: datetime = Field(..., description="Start of the window under study")
    window_hours: int = Field(default=48, ge=1, le=336)
    reduction_pct: float = Field(default=0.25, gt=0, lt=1,
                                 description="Productivity cut as a fraction (0.25 = 25%)")
    vessel_name: Optional[str] = Field(default=None, max_length=200)
    voyage_number: Optional[str] = Field(default=None, max_length=64)
    berthing_record_id: Optional[int] = Field(default=None, ge=1)

    model_config = ConfigDict(json_schema_extra={"example": {
        "as_of": "2026-08-06T00:00:00Z", "reduction_pct": 0.25}})


class ModalShiftIn(BaseModel):
    """Scenario II-A."""
    from_date: date
    to_date: date
    shift_pct: float = Field(default=0.20, gt=0, le=1,
                             description="Share of rail volume moved to road")
    terminal: Optional[str] = Field(default=None, max_length=64)
    gate_id: Optional[str] = Field(default=None, max_length=64)
    sustained_rate: Optional[float] = Field(
        default=None, gt=0,
        description="Override the gate's sustained trucks/hour instead of deriving it")

    model_config = ConfigDict(json_schema_extra={"example": {
        "from_date": "2026-08-01", "to_date": "2026-08-03", "shift_pct": 0.20}})

    @field_validator("to_date")
    @classmethod
    def _v_window(cls, v: date, info) -> date:
        start = info.data.get("from_date")
        if start and v < start:
            raise ValueError("to_date must not precede from_date")
        if start and (v - start).days > _MAX_WINDOW_DAYS:
            raise ValueError(f"window must not exceed {_MAX_WINDOW_DAYS} days")
        return v


class GateSlottingIn(BaseModel):
    """Scenario III-A."""
    from_ts: datetime
    to_ts: datetime
    terminal: Optional[str] = Field(default=None, max_length=64)
    gate_id: Optional[str] = Field(default=None, max_length=64)
    sustained_rate: Optional[float] = Field(default=None, gt=0)

    model_config = ConfigDict(json_schema_extra={"example": {
        "from_ts": "2026-08-01T00:00:00Z", "to_ts": "2026-08-02T00:00:00Z"}})

    @field_validator("to_ts")
    @classmethod
    def _v_window(cls, v: datetime, info) -> datetime:
        start = info.data.get("from_ts")
        if start and v <= start:
            raise ValueError("to_ts must be after from_ts")
        return v


class DriverShortageIn(BaseModel):
    """Scenario III-B."""
    from_date: date
    to_date: date
    state_date: Optional[date] = Field(
        default=None, description="Day the backlog is reported for (default to_date + 1)")
    reduction_pct: float = Field(default=1.0 / 3.0, gt=0, lt=1)

    model_config = ConfigDict(json_schema_extra={"example": {
        "from_date": "2026-08-01", "to_date": "2026-08-03",
        "state_date": "2026-08-04"}})

    @field_validator("to_date")
    @classmethod
    def _v_window(cls, v: date, info) -> date:
        start = info.data.get("from_date")
        if start and v < start:
            raise ValueError("to_date must not precede from_date")
        if start and (v - start).days > _MAX_WINDOW_DAYS:
            raise ValueError(f"window must not exceed {_MAX_WINDOW_DAYS} days")
        return v


# ------------------------------------------------------------------------ helpers
async def _run(service: SimulationService, scenario: str, params: dict) -> dict:
    try:
        return await service.run(scenario, params)
    except SimulationError as exc:
        REQUESTS.labels("cargo_simulation", "bad_request").inc()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_scenario_request",
                                    "scenario": scenario, "message": str(exc)})
    finally:
        REQUESTS.labels("cargo_simulation", "ok").inc()


# ---------------------------------------------------------------------- endpoints
@router.get("/api/cargo/simulate/scenarios",
            summary="What-if scenarios this backend can answer, and how to ask")
async def list_scenarios(service: SimulationService = Depends(get_service)) -> dict:
    """The catalog: one entry per scenario with its JNPA reference, the question it
    answers, its parameters and the tables it reads."""
    return {"count": len(service.catalog()), "scenarios": service.catalog(),
            "contract": {
                "method": "how the figure was computed, in words",
                "result": "the answer, with its supporting detail",
                "figures": "the headline numbers",
                "assumptions": "every assumption, stated separately from the result",
                "queries": "the SQL and bound parameters behind the answer",
                "recommendations": "what to do about it",
                "data_available": ("false when a required input table was empty — "
                                   "the scenario reports that rather than "
                                   "inventing a figure")}}


@router.post("/api/cargo/simulate/vessel-bunching",
             summary="I-A — vessel bunching: berthing order against a stated objective")
async def simulate_vessel_bunching(
    body: VesselBunchingIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    params = body.model_dump()
    params["as_of"] = _utc(body.as_of)
    return await _run(service, "vessel-bunching", params)


@router.post("/api/cargo/simulate/berth-cascade",
             summary="I-B — extended berth window: 48h queue displacement")
async def simulate_berth_cascade(
    body: BerthCascadeIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    params = body.model_dump()
    params["as_of"] = _utc(body.as_of)
    return await _run(service, "berth-cascade", params)


@router.post("/api/cargo/simulate/crane-productivity",
             summary="II-B — equipment availability: gross moves/hour and a 25% cut")
async def simulate_crane_productivity(
    body: CraneProductivityIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    params = body.model_dump()
    params["as_of"] = _utc(body.as_of)
    return await _run(service, "crane-productivity", params)


@router.post("/api/cargo/simulate/modal-shift",
             summary="II-A — rail to road: hourly gate profile before and after")
async def simulate_modal_shift(
    body: ModalShiftIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    return await _run(service, "modal-shift", body.model_dump())


@router.post("/api/cargo/simulate/gate-slotting",
             summary="III-A — gate approach congestion and a slotting proposal")
async def simulate_gate_slotting(
    body: GateSlottingIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    params = body.model_dump()
    params["from_ts"] = _utc(body.from_ts)
    params["to_ts"] = _utc(body.to_ts)
    return await _run(service, "gate-slotting", params)


@router.post("/api/cargo/simulate/driver-shortage",
             summary="III-B — trips per vehicle cut by a third: throughput + exposure")
async def simulate_driver_shortage(
    body: DriverShortageIn,
    service: SimulationService = Depends(get_service),
) -> dict:
    return await _run(service, "driver-shortage", body.model_dump())


@router.get("/api/gate/hourly-profile",
            summary="Hourly truck arrivals at the gate over any window")
async def gate_hourly_profile(
    from_ts: datetime = Query(..., alias="from",
                              description="Window start (ISO-8601, inclusive)"),
    to_ts: datetime = Query(..., alias="to",
                            description="Window end (ISO-8601, exclusive)"),
    terminal: Optional[str] = Query(default=None),
    gate_id: Optional[str] = Query(default=None),
    group_by: str = Query(default="hour", pattern="^(hour|day)$"),
    service: SimulationService = Depends(get_service),
) -> dict:
    """Real hourly gate arrivals for an ARBITRARY historical window, from
    ``core.eir.truck_in_time`` with ``core.gate_event`` as fallback.

    This is the endpoint the audit found missing. ``mart.v_gate_throughput`` — the
    only hourly gate view that existed — is defined with a hardcoded
    ``WHERE ts > now() - '24:00:00'`` and therefore cannot address a past date at
    all, which blocked both II-A and III-A. Returns the same query traces the
    simulate endpoints do, so the working stays citable (Notice §1.d)."""
    start, end = _utc(from_ts), _utc(to_ts)
    if end <= start:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_window",
                                    "message": "`to` must be after `from`"})
    if (end - start).days > _MAX_WINDOW_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_window",
                                    "message": f"window must not exceed "
                                               f"{_MAX_WINDOW_DAYS} days"})
    # Reuses the scenario's own loader, so this endpoint and III-A can never
    # disagree about what the arrival profile for a window is.
    from services.cargo.simulation.gate_slotting import load_profile

    profile, traces, assumptions, source = await load_profile(
        service._repo, from_ts=start, to_ts=end, terminal=terminal, gate_id=gate_id)

    if group_by == "day":
        daily: dict[Any, dict] = {}
        for hour in profile:
            key = hour["bucket"].date() if hasattr(hour["bucket"], "date") else hour["bucket"]
            agg = daily.setdefault(key, {"bucket": key, "arrivals": 0,
                                         "completed": 0, "unique_trucks": 0})
            agg["arrivals"] += hour["arrivals"]
            agg["completed"] += hour.get("completed") or 0
            # Distinct trucks cannot be summed across hours without the identities;
            # the max is the honest lower bound and is labelled as such.
            agg["unique_trucks"] = max(agg["unique_trucks"], hour.get("unique_trucks") or 0)
        buckets = sorted(daily.values(), key=lambda b: b["bucket"])
    else:
        buckets = profile

    total = sum(b["arrivals"] for b in buckets)
    peak = max(buckets, key=lambda b: b["arrivals"]) if buckets else None
    REQUESTS.labels("cargo_simulation", "ok").inc()
    return {
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "group_by": group_by,
        "source": source,
        "count": len(buckets),
        "total_arrivals": total,
        "peak_bucket": (peak["bucket"].isoformat() if peak and hasattr(peak["bucket"], "isoformat")
                        else (peak["bucket"] if peak else None)),
        "peak_arrivals": peak["arrivals"] if peak else 0,
        "mean_per_bucket": round(total / len(buckets), 2) if buckets else 0.0,
        "buckets": [
            {**b, "bucket": b["bucket"].isoformat() if hasattr(b["bucket"], "isoformat")
             else b["bucket"]}
            for b in buckets],
        "notes": (["unique_trucks on a daily bucket is the busiest hour's distinct "
                   "count, not a daily distinct count — the hourly rollup does not "
                   "carry truck identities"] if group_by == "day" else []),
        "assumptions": [a.to_dict() for a in assumptions],
        "queries": [t.to_dict() for t in traces],
    }


@router.post("/api/cargo/simulate/{scenario}",
             summary="Run any registered what-if scenario by name")
async def simulate(
    scenario: str,
    body: dict = Body(default_factory=dict),
    service: SimulationService = Depends(get_service),
) -> dict:
    """Generic passthrough for the registry. The typed endpoints above are the
    documented surface (they validate their parameters); this exists so a new
    scenario is reachable the moment it is registered.

    Date/timestamp parameters must be ISO-8601 strings and are coerced here — the
    scenario modules work in real ``datetime``/``date`` objects, never strings."""
    params = dict(body or {})
    for key in ("as_of", "from_ts", "to_ts"):
        if isinstance(params.get(key), str):
            try:
                params[key] = _utc(datetime.fromisoformat(params[key].replace("Z", "+00:00")))
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_timestamp", "field": key,
                            "message": f"{key} must be ISO-8601"})
    for key in ("from_date", "to_date", "state_date"):
        if isinstance(params.get(key), str):
            try:
                params[key] = date.fromisoformat(params[key])
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"error": "invalid_date", "field": key,
                            "message": f"{key} must be an ISO-8601 date"})
    return await _run(service, scenario, params)
