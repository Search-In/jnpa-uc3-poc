"""CFS-ECY CODECO service orchestration — the single read entry point.

Thin over :class:`CfsEcyRepository`: owns observability (one structured log line
per op) and shapes the stats / timeline envelopes, keeping the router free of SQL.
Stateless apart from the DSN (one shared instance is safe), mirroring
services.cargo / services.driver_master. The repository is dependency-injected so
tests can pass a fake.

Read-only + additive: nothing here writes to any existing table. The container
timeline enriches CODECO events with the EXISTING Container Lifecycle status via a
soft, best-effort read of jnpa.cargo (never required, never mutated).
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, List, Mapping, Optional

from jnpa_shared.logging import get_logger

from .repository import CfsEcyRepository

log = get_logger("services.cfs_ecy.service")


class CfsEcyService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[CfsEcyRepository] = None) -> None:
        self._repo = repository or CfsEcyRepository(dsn=dsn)

    # ----------------------------------------------------------------- list
    async def list_movements(self, filters: Mapping[str, Any], *, sort: str,
                             direction: str, limit: int, offset: int) -> Dict[str, Any]:
        t0 = perf_counter()
        rows = await self._repo.list_movements(
            filters, sort=sort, direction=direction, limit=limit, offset=offset)
        total = await self._repo.count(filters)
        log.info("cfs_ecy.list", extra={"total": total, "returned": len(rows),
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    # ----------------------------------------------------------------- stats
    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        t0 = perf_counter()
        base = await self._repo.stats(filters)
        dwell = await self._repo.dwell_summary(filters)
        throughput = await self._repo.daily_throughput(filters)
        log.info("cfs_ecy.stats", extra={"ms": round((perf_counter() - t0) * 1000, 1)})
        return {
            "total_in": base["total_in"],
            "total_out": base["total_out"],
            "total_events": base["total_events"],
            "container_count": base["container_count"],
            "active_containers": base["active_containers"],
            "iso_invalid": base["iso_invalid"],
            "average_dwell_hours": dwell["average_dwell_hours"],
            "median_dwell_hours": dwell["median_dwell_hours"],
            "dwell_count": dwell["dwell_count"],
            "daily_throughput": throughput,
        }

    # --------------------------------------------------------------- timeline
    async def container_timeline(self, container_number: str) -> Optional[Dict[str, Any]]:
        """CODECO timeline + dwell + (soft) cargo lifecycle for one container.
        Returns None when the container has no CODECO events (router -> 404)."""
        cn = (container_number or "").strip().upper()
        events = await self._repo.container_events(cn)
        if not events:
            return None
        dwell = await self._repo.container_dwell(cn)
        cargo = await self._repo.cargo_lifecycle(cn)
        iso_valid = bool(events[0].get("iso_valid"))
        # A single CFS dwell figure for the header, if present.
        cfs_dwell = next((d.get("dwell_hours") for d in dwell
                          if d.get("facility_type") == "CFS" and d.get("dwell_hours") is not None), None)
        return {
            "container_number": cn,
            "iso_valid": iso_valid,
            "events": events,
            "dwell": dwell,
            "dwell_hours": cfs_dwell,
            "cargo": cargo,          # None when not tracked by Container Lifecycle
            "in_lifecycle": cargo is not None,
        }

    # ------------------------------------------------------------ dwell report
    async def dwell_report(self, filters: Mapping[str, Any], *, limit: int,
                           offset: int) -> Dict[str, Any]:
        rows, total = await self._repo.dwell_report(filters, limit=limit, offset=offset)
        summary = await self._repo.dwell_summary({"facility_type": "CFS"})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows), "summary": summary,
                "note": "Dwell is computed for CFS only; ECY containers have a "
                        "single CODECO event and no paired In/Out, so ECY dwell is "
                        "not fabricated."}
