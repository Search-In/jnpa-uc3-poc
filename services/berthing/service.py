"""Berthing Reports read orchestration (UC-III module 7).

Thin over :class:`BerthingRepository`: list/search/paginate vessel calls, one call
with its lifecycle timeline, and KPI aggregates for the dashboard. Read-only; the
write path lives in :class:`BerthingUploadService`.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from jnpa_shared.logging import get_logger

from services.marine.projection import MarineProjection

from . import lifecycle as lc
from .repository import BerthingRepository

log = get_logger("services.berthing.service")


class BerthingService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[BerthingRepository] = None,
                 projection: Optional[MarineProjection] = None) -> None:
        self._repo = repository or BerthingRepository(dsn)
        # Lifecycle comes from the shared Marine Projection Layer — this module owns no
        # query against core.vessel_call* and no call to the state engine.
        self._projection = projection or MarineProjection(dsn)

    async def _advance(self, rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """Advance each row's status with the marine lifecycle, where one exists.

        The row set is unchanged — same rows, same keys, same order — so the API response
        shape is byte-identical. Only the VALUE of ``status`` can move, and only FORWARD:
        :func:`lifecycle.effective_status` keeps whichever of the stored and derived values
        is further along berthing's own ladder. A report whose VIA matches no call is
        returned exactly as the repository produced it.

        One extra round trip per page, batched over the page's VIAs — never per row.
        """
        if not rows:
            return rows
        projections = await self._projection.by_vias(
            [r.get("voyage_number") for r in rows])
        if not projections:
            return rows
        return [lc.apply(r, projections.get(str(r.get("voyage_number") or "")))
                for r in rows]

    async def list_reports(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_reports(filters, sort=sort, direction=direction,
                                              limit=limit, offset=offset)
        items = await self._advance(items)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, report_id: int) -> Optional[Dict[str, Any]]:
        row = await self._repo.get(report_id)
        if row is None:
            return None
        return (await self._advance([row]))[0]

    async def timeline(self, report_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.timeline(report_id)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
