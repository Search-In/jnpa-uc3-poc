"""UC-I Marine vessel-call read orchestration.

Thin over :class:`VesselCallRepository`: list/search/paginate vessel calls, resolve a
call by VCN or short VIA, one call with its ordered actuals, and KPI aggregates for the
dashboard. Read-only; the ingestion write path lands in a later slice as a separate
upload service, exactly as :class:`services.berthing.BerthingUploadService` does for
berthing.

No business logic lives here — the envelope assembly below is the same shape
:class:`services.berthing.BerthingService` produces, so routers stay declarative.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import VesselCallRepository

log = get_logger("services.marine.service")


class VesselCallService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VesselCallRepository] = None) -> None:
        self._repo = repository or VesselCallRepository(dsn)

    async def list_calls(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                         limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_calls(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def count(self, filters: Mapping[str, Any]) -> int:
        return await self._repo.count(filters)

    async def get(self, call_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get(call_id)

    async def get_by_vcn(self, vcn: str) -> Optional[Dict[str, Any]]:
        return await self._repo.get_by_vcn(vcn)

    async def get_by_via(self, via_no: str) -> List[Dict[str, Any]]:
        """A short VIA may resolve to several calls — see VesselCallRepository."""
        return await self._repo.get_by_via(via_no)

    async def timeline(self, call_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.timeline(call_id)

    async def list_events(self, call_id: int, *, limit: int,
                          offset: int) -> List[Dict[str, Any]]:
        return await self._repo.list_events(call_id, limit=limit, offset=offset)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
