"""Berthing Reports read orchestration (UC-III module 7).

Thin over :class:`BerthingRepository`: list/search/paginate vessel calls, one call
with its lifecycle timeline, and KPI aggregates for the dashboard. Read-only; the
write path lives in :class:`BerthingUploadService`.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import BerthingRepository

log = get_logger("services.berthing.service")


class BerthingService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[BerthingRepository] = None) -> None:
        self._repo = repository or BerthingRepository(dsn)

    async def list_reports(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_reports(filters, sort=sort, direction=direction,
                                              limit=limit, offset=offset)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, report_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get(report_id)

    async def timeline(self, report_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.timeline(report_id)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
