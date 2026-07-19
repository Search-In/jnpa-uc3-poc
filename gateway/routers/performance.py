"""/api/performance — Performance & Daily Reports (UC-III module 12, additive, read-only).

A thin router over :class:`services.performance.PerformanceService` (service ->
raw-SQL PerformanceRepository), in the same mould as gateway/routers/cfs_ecy.py.
It reads the official JNPA performance report tables (jnpa.perf_*) populated by
scripts/import_performance_reports.py from the Daily Status Report, monthly JN
Port TEUs, and NLDS/LDB Analytics PDFs. It writes nothing and touches no existing
table — auth / JWT / RBAC / cargo / cfs_ecy / vehicle / driver / transporter /
ldb_movements are all untouched.

    GET /api/performance/terminals              -> canonical terminal dimension
    GET /api/performance/meta                   -> available report dates + LDB months
    GET /api/performance/kpi                    -> headline KPIs + day-over-day deltas
    GET /api/performance/daily                  -> full daily report bundle for a date
    GET /api/performance/daily/traffic          -> container TEUs + rail (list/filter)
    GET /api/performance/daily/status           -> pendency/yard/gate/reefer snapshot
    GET /api/performance/daily/vessels          -> vessels under operation
    GET /api/performance/monthly-teu            -> monthly JN Port TEUs
    GET /api/performance/trends                 -> time series for a chosen metric
    GET /api/performance/stats                  -> overview aggregate (daily series + KPI)
    GET /api/performance/dwell                  -> LDB port dwell time
    GET /api/performance/cfs-icd                -> LDB CFS/ICD facility dwell
    GET /api/performance/congestion             -> LDB congestion clusters
    GET /api/performance/routes                 -> LDB route modal share
    GET /api/performance/weather                -> LDB weather-conditioned dwell

RBAC: /api/performance is not in gateway/auth.py._POLICY, so it inherits the
default "any authenticated role" rule (read-only). No auth change is required
or made. Prefix deliberately avoids the pre-existing /api/reports router.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.performance import PerformanceService

router = APIRouter(prefix="/api/performance", tags=["performance"])

_service: Optional[PerformanceService] = None


def get_service(request: Request) -> PerformanceService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = PerformanceService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# --------------------------------------------------------------------- DTOs
class ListResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: List[Dict[str, Any]]
    total: Optional[int] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    count: int


# ------------------------------------------------------------------- helpers
_PERIODS = ("DAY", "MONTH", "YEAR")
_CYCLES = ("IMPORT", "EXPORT")


def _upper_in(value: Optional[str], allowed: tuple, name: str) -> Optional[str]:
    if value is None:
        return None
    v = value.strip().upper()
    if v not in allowed:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": f"invalid_{name}", name: value})
    return v


def _terminal(value: Optional[str]) -> Optional[str]:
    return value.strip().upper() if value else None


# ------------------------------------------------------------------- meta
@router.get("/terminals", response_model=ListResponse, summary="Canonical terminal dimension")
async def terminals(service: PerformanceService = Depends(get_service)) -> ListResponse:
    res = await service.terminals()
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


@router.get("/meta", summary="Available report dates + LDB months")
async def meta(service: PerformanceService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("performance", "ok").inc()
    return await service.meta()


@router.get("/kpi", summary="Headline KPIs + day-over-day deltas for a report date (latest if omitted)")
async def kpi(date_: Optional[date] = Query(default=None, alias="date"),
              service: PerformanceService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.kpi(date_)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "no_daily_reports"})
    REQUESTS.labels("performance", "ok").inc()
    return res


# ------------------------------------------------------------------- daily
@router.get("/daily", summary="Full daily report bundle (all sections) for a date")
async def daily(date_: date = Query(..., alias="date"),
                service: PerformanceService = Depends(get_service)) -> Dict[str, Any]:
    res = await service.daily_bundle(date_)
    if res is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "report_not_found", "date": str(date_)})
    REQUESTS.labels("performance", "ok").inc()
    return res


@router.get("/daily/traffic", response_model=ListResponse,
            summary="Container TEUs + rail operations (list / filter / paginate)")
async def daily_traffic(
    date_from: Optional[date] = Query(default=None, alias="from"),
    date_to: Optional[date] = Query(default=None, alias="to"),
    terminal: Optional[str] = Query(default=None),
    period: Optional[str] = Query(default=None, description="DAY | MONTH | YEAR"),
    sort: str = Query(default="report_date"),
    direction: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PerformanceService = Depends(get_service),
) -> ListResponse:
    filters = {"date_from": date_from, "date_to": date_to, "terminal": _terminal(terminal),
               "period": _upper_in(period, _PERIODS, "period")}
    res = await service.list_traffic(filters, sort=sort, direction=direction,
                                     limit=limit, offset=offset)
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


@router.get("/daily/status", response_model=ListResponse,
            summary="Import pendency / yard / gate / reefer snapshot")
async def daily_status(
    date_: Optional[date] = Query(default=None, alias="date"),
    terminal: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PerformanceService = Depends(get_service),
) -> ListResponse:
    res = await service.list_status({"date": date_, "terminal": _terminal(terminal)},
                                    limit=limit, offset=offset)
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


@router.get("/daily/vessels", response_model=ListResponse, summary="Vessels under operation")
async def daily_vessels(
    date_: Optional[date] = Query(default=None, alias="date"),
    terminal: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PerformanceService = Depends(get_service),
) -> ListResponse:
    res = await service.list_vessels({"date": date_, "terminal": _terminal(terminal)},
                                     limit=limit, offset=offset)
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


@router.get("/monthly-teu", response_model=ListResponse, summary="Monthly JN Port TEUs (per terminal)")
async def monthly_teu(
    fiscal_year: Optional[str] = Query(default=None),
    terminal: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None, alias="from"),
    date_to: Optional[date] = Query(default=None, alias="to"),
    sort: str = Query(default="month_date"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: PerformanceService = Depends(get_service),
) -> ListResponse:
    filters = {"fiscal_year": fiscal_year, "terminal": _terminal(terminal),
               "date_from": date_from, "date_to": date_to}
    res = await service.list_monthly(filters, sort=sort, direction=direction,
                                     limit=limit, offset=offset)
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


# ------------------------------------------------------------------- trends / stats
@router.get("/trends", summary="Time series for a chosen metric")
async def trends(
    metric: str = Query(default="total_teus"),
    grain: str = Query(default="daily", description="daily | monthly"),
    terminal: Optional[str] = Query(default=None),
    date_from: Optional[date] = Query(default=None, alias="from"),
    date_to: Optional[date] = Query(default=None, alias="to"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    g = grain.strip().lower()
    if g not in ("daily", "monthly"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_grain", "grain": grain})
    res = await service.trends(metric.strip(), grain=g, terminal=_terminal(terminal),
                               date_from=date_from, date_to=date_to)
    REQUESTS.labels("performance", "ok").inc()
    return res


@router.get("/stats", summary="Overview aggregate: daily headline series + latest KPI")
async def stats(
    date_from: Optional[date] = Query(default=None, alias="from"),
    date_to: Optional[date] = Query(default=None, alias="to"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    res = await service.stats(date_from, date_to)
    REQUESTS.labels("performance", "ok").inc()
    return res


# ------------------------------------------------------------------- LDB
@router.get("/dwell", summary="LDB port dwell time (by terminal / cycle / segment)")
async def dwell(
    report_month: Optional[date] = Query(default=None, alias="month"),
    terminal: Optional[str] = Query(default=None),
    cycle: Optional[str] = Query(default=None, description="IMPORT | EXPORT"),
    segment: Optional[str] = Query(default=None, description="OVERALL | TRUCK | TRAIN"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    filters = {"report_month": report_month, "terminal_code": _terminal(terminal),
               "cycle": _upper_in(cycle, _CYCLES, "cycle"),
               "segment": _upper_in(segment, ("OVERALL", "TRUCK", "TRAIN"), "segment")}
    res = await service.ldb_dwell(filters)
    REQUESTS.labels("performance", "ok").inc()
    return res


@router.get("/cfs-icd", response_model=ListResponse, summary="LDB CFS/ICD facility dwell")
async def cfs_icd(
    report_month: Optional[date] = Query(default=None, alias="month"),
    facility_type: Optional[str] = Query(default=None, description="CFS | ICD"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: PerformanceService = Depends(get_service),
) -> ListResponse:
    filters = {"report_month": report_month,
               "facility_type": _upper_in(facility_type, ("CFS", "ICD"), "facility_type")}
    res = await service.ldb_facility(filters, limit=limit, offset=offset)
    REQUESTS.labels("performance", "ok").inc()
    return ListResponse(**res)


@router.get("/congestion", summary="LDB congestion clusters")
async def congestion(
    report_month: Optional[date] = Query(default=None, alias="month"),
    cycle: Optional[str] = Query(default=None, description="IMPORT | EXPORT"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    filters = {"report_month": report_month, "cycle": _upper_in(cycle, _CYCLES, "cycle")}
    res = await service.ldb_congestion(filters)
    REQUESTS.labels("performance", "ok").inc()
    return res


@router.get("/routes", summary="LDB container-movement route modal share")
async def routes(
    report_month: Optional[date] = Query(default=None, alias="month"),
    cycle: Optional[str] = Query(default=None, description="IMPORT | EXPORT"),
    transport_mode: Optional[str] = Query(default=None, description="TRAIN | TRUCK"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    filters = {"report_month": report_month, "cycle": _upper_in(cycle, _CYCLES, "cycle"),
               "transport_mode": _upper_in(transport_mode, ("TRAIN", "TRUCK"), "transport_mode")}
    res = await service.ldb_routes(filters)
    REQUESTS.labels("performance", "ok").inc()
    return res


@router.get("/weather", summary="LDB weather-conditioned dwell (terminal-wise)")
async def weather(
    report_month: Optional[date] = Query(default=None, alias="month"),
    terminal: Optional[str] = Query(default=None),
    cycle: Optional[str] = Query(default=None, description="IMPORT | EXPORT"),
    service: PerformanceService = Depends(get_service),
) -> Dict[str, Any]:
    filters = {"report_month": report_month, "terminal_code": _terminal(terminal),
               "cycle": _upper_in(cycle, _CYCLES, "cycle")}
    res = await service.ldb_weather(filters)
    REQUESTS.labels("performance", "ok").inc()
    return res
