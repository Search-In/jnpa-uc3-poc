"""Performance & Daily Reports service orchestration (Module 12).

Thin over :class:`PerformanceRepository`: owns observability (one structured log
line per op) and shapes the response envelopes, keeping the router free of SQL.
Stateless apart from the DSN; the repository is dependency-injected so tests can
pass a fake. Read-only + additive — nothing here writes to any table.
"""
from __future__ import annotations

from datetime import date
from time import perf_counter
from typing import Any, Dict, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import PerformanceRepository

log = get_logger("services.performance.service")


class PerformanceService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[PerformanceRepository] = None) -> None:
        self._repo = repository or PerformanceRepository(dsn=dsn)

    # ---------------------------------------------------------------- meta
    async def terminals(self) -> Dict[str, Any]:
        items = await self._repo.terminals()
        return {"items": items, "count": len(items)}

    async def meta(self) -> Dict[str, Any]:
        return {
            "report_dates": await self._repo.report_dates(),
            "latest_report_date": (str(await self._repo.latest_report_date())
                                   if await self._repo.latest_report_date() else None),
            "ldb_months": await self._repo.ldb_months(),
        }

    # ---------------------------------------------------------------- KPI
    async def kpi(self, report_date: Optional[date]) -> Optional[Dict[str, Any]]:
        t0 = perf_counter()
        res = await self._repo.kpi(report_date)
        log.info("performance.kpi", extra={"ms": round((perf_counter() - t0) * 1000, 1),
                 "date": str(report_date) if report_date else "latest"})
        return res

    # ---------------------------------------------------------------- daily
    async def daily_bundle(self, d: date) -> Optional[Dict[str, Any]]:
        return await self._repo.daily_bundle(d)

    async def list_traffic(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> Dict[str, Any]:
        rows, total = await self._repo.list_traffic(
            filters, sort=sort, direction=direction, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def list_status(self, filters, *, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.list_status(filters, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def list_vessels(self, filters, *, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.list_vessels(filters, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def list_monthly(self, filters, *, sort, direction, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.list_monthly(
            filters, sort=sort, direction=direction, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    # ---------------------------------------------------------------- trends / stats
    async def trends(self, metric: str, *, grain: str, terminal: Optional[str],
                     date_from, date_to) -> Dict[str, Any]:
        series = await self._repo.trends(metric, grain=grain, terminal=terminal,
                                         date_from=date_from, date_to=date_to)
        return {"metric": metric, "grain": grain, "terminal": terminal, "series": series,
                "count": len(series)}

    async def stats(self, date_from, date_to) -> Dict[str, Any]:
        t0 = perf_counter()
        daily = await self._repo.daily_series(date_from, date_to)
        kpi = await self._repo.kpi(None)
        log.info("performance.stats", extra={"ms": round((perf_counter() - t0) * 1000, 1),
                 "days": len(daily)})
        return {"daily": daily, "latest_kpi": kpi, "days": len(daily)}

    # ---------------------------------------------------------------- LDB
    async def ldb_dwell(self, filters) -> Dict[str, Any]:
        port = await self._repo.ldb_port_dwell(filters)
        return {"items": port, "count": len(port)}

    async def ldb_facility(self, filters, *, limit, offset) -> Dict[str, Any]:
        rows, total = await self._repo.ldb_facility_dwell(filters, limit=limit, offset=offset)
        return {"items": rows, "total": total, "limit": limit, "offset": offset, "count": len(rows)}

    async def ldb_congestion(self, filters) -> Dict[str, Any]:
        rows = await self._repo.ldb_congestion(filters)
        return {"items": rows, "count": len(rows)}

    async def ldb_routes(self, filters) -> Dict[str, Any]:
        rows = await self._repo.ldb_routes(filters)
        return {"items": rows, "count": len(rows)}

    async def ldb_weather(self, filters) -> Dict[str, Any]:
        rows = await self._repo.ldb_weather(filters)
        return {"items": rows, "count": len(rows)}
