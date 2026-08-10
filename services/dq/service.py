"""Data Quality ledger orchestration — the single read entry point.

Thin over :class:`DqRepository`: owns observability and shapes the list/summary
envelopes so the router carries no SQL. Stateless apart from the DSN.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import DqRepository

log = get_logger("services.dq.service")


class DqService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[DqRepository] = None) -> None:
        self._repo = repository or DqRepository(dsn=dsn)

    async def list_issues(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> Dict[str, Any]:
        t0 = perf_counter()
        rows = await self._repo.list_issues(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count_issues(filters)
        log.info("dq.list", extra={"total": total, "returned": len(rows),
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def summary(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        """Ledger roll-up: totals by severity, by source table and by issue type."""
        t0 = perf_counter()
        total = await self._repo.count_issues(filters)
        severities = {r["severity"]: r["issues"]
                      for r in await self._repo.by_severity(filters)}
        by_source = await self._repo.by_source_table(filters)
        by_type = await self._repo.by_issue_type(filters)
        log.info("dq.summary", extra={"total": total,
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {
            "total": total,
            "errors": severities.get("error", 0),
            "warnings": severities.get("warn", 0),
            "info": severities.get("info", 0),
            "by_source_table": by_source,
            "by_issue_type": by_type,
        }
