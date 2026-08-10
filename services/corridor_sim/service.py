"""Corridor-simulation orchestration (UC3-005).

Shapes the dashboard envelope. Every payload carries ``simulated: true``,
``provenance: 'SIMULATED'`` and the reproducibility triple (seed, seed_version,
config_sha256) so the UI can label the data and a reader can verify the run was
not reseeded after rehearsal.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from .repository import DEFAULT_RUN_ID, CorridorSimRepository

log = get_logger("services.corridor_sim.service")


class CorridorSimService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[CorridorSimRepository] = None) -> None:
        self._repo = repository or CorridorSimRepository(dsn=dsn)

    async def summary(self, run_id: str = DEFAULT_RUN_ID) -> Optional[Dict[str, Any]]:
        t0 = perf_counter()
        run = await self._repo.run(run_id)
        if run is None:
            return None
        total = await self._repo.truck_total(run_id)
        by_dir = await self._repo.by_direction(run_id)
        segments = await self._repo.by_segment(run_id)
        states = await self._repo.by_state(run_id)
        log.info("corridor_sim.summary", extra={"run": run_id, "trucks": total,
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {
            "run": run,
            "simulated": True,
            "provenance": "SIMULATED",
            "trucks_total": total,
            "inbound": by_dir.get("IN", 0),
            "outbound": by_dir.get("OUT", 0),
            "segments": segments,
            "segment_count": len(segments),
            "states": states,
            "calibration": {
                "anchor_date": run["anchor_date"],
                "anchor_in_teu": run["anchor_in_teu"],
                "anchor_out_teu": run["anchor_out_teu"],
                "anchor_total_teu": run["anchor_total_teu"],
                "window_from": run["calibration_from"],
                "window_to": run["calibration_to"],
                "note": run["calibration_note"],
            },
            "reproducibility": {
                "seed": run["seed"],
                "seed_version": run["seed_version"],
                "config_sha256": run["config_sha256"],
            },
        }

    async def trucks(self, run_id: str = DEFAULT_RUN_ID, *, segment: Optional[str] = None,
                     direction: Optional[str] = None, limit: int = 50,
                     offset: int = 0) -> Dict[str, Any]:
        rows = await self._repo.trucks(run_id, segment=segment, direction=direction,
                                       limit=limit, offset=offset)
        return {"items": rows, "count": len(rows), "limit": limit, "offset": offset,
                "simulated": True, "provenance": "SIMULATED"}
