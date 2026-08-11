"""Corridor-simulation persistence (UC3-005) — read-only over the sim tables.

``core.sim_run`` / ``core.sim_truck`` are written by
scripts/seed_uc3_005_corridor_simulation.py. Nothing here writes. Both tables
pin ``simulated = true`` and ``provenance = 'SIMULATED'`` with CHECK constraints
(migration 0135), so every row this repository can possibly return is generated
data — there is no code path by which a measured observation reaches it.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.corridor_sim.repository")

DEFAULT_RUN_ID = "uc3-005-nh348-20k"


class CorridorSimRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def run(self, run_id: str) -> Optional[dict]:
        rows = await self._rows(
            "SELECT run_id, corridor, seed, seed_version, config_sha256, truck_count, "
            "       segment_count, calibration_from, calibration_to, anchor_date, "
            "       anchor_in_teu, anchor_out_teu, anchor_total_teu, calibration_note, "
            "       frozen_at, simulated "
            "  FROM core.sim_run WHERE run_id = :r", {"r": run_id})
        return rows[0] if rows else None

    async def truck_total(self, run_id: str) -> int:
        rows = await self._rows(
            "SELECT count(*) AS n FROM core.sim_truck WHERE run_id = :r", {"r": run_id})
        return int(rows[0]["n"]) if rows else 0

    async def by_segment(self, run_id: str) -> list[dict]:
        return await self._rows(
            "SELECT segment_code, count(*) AS trucks, "
            "       count(*) FILTER (WHERE direction='IN')  AS inbound, "
            "       count(*) FILTER (WHERE direction='OUT') AS outbound "
            "  FROM core.sim_truck WHERE run_id = :r "
            " GROUP BY segment_code ORDER BY segment_code", {"r": run_id})

    async def by_direction(self, run_id: str) -> dict:
        rows = await self._rows(
            "SELECT direction, count(*) AS trucks FROM core.sim_truck "
            " WHERE run_id = :r GROUP BY direction", {"r": run_id})
        return {r["direction"]: int(r["trucks"]) for r in rows}

    async def by_state(self, run_id: str) -> list[dict]:
        return await self._rows(
            "SELECT state, count(*) AS trucks FROM core.sim_truck "
            " WHERE run_id = :r GROUP BY state ORDER BY state", {"r": run_id})

    async def trucks(self, run_id: str, *, segment: Optional[str], direction: Optional[str],
                     limit: int, offset: int) -> list[dict]:
        conds, p = ["run_id = :r"], {"r": run_id, "limit": limit, "offset": offset}
        if segment:
            conds.append("segment_code = :seg")
            p["seg"] = segment
        if direction:
            conds.append("direction = :dir")
            p["dir"] = direction
        return await self._rows(
            "SELECT truck_uid, truck_no, segment_code, direction, state, replay_ts, "
            "       simulated, provenance FROM core.sim_truck WHERE "
            + " AND ".join(conds) +
            " ORDER BY truck_uid LIMIT :limit OFFSET :offset", p)
