"""Gate & lane board persistence (UC3-021) and CPP metered release (UC3-027).

The ONLY layer that speaks SQL for the two boards. Read-mostly; the two writes
are the lane-reassignment TASK and the CPP release plan, both of which are
records of a decision, never a command to equipment.

The load-bearing rule of this module is WHERE THE QUEUE COMES FROM.

``queue_at_gates`` reads ``core.camera_ai_count.queue_count`` — vehicles counted
in the queue zone of a camera frame — and reads NOTHING from
``core.gate_event``. ``throughput_at_gates`` reads ``core.gate_event`` and
reads NOTHING from ``core.camera_ai_count``. The two are separate queries on
separate tables by design, so stopping a gate drives throughput to zero while
the counted queue keeps climbing (UI-068). A single query that computed both
would make that behaviour an accident; two make it structural.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.gate_board.repository")

#: Queue-counting methods the board will render. Mirrors core.queue_count_method
#: (migration 0136), which deliberately has no THROUGHPUT_DERIVED row.
ACCEPTED_QUEUE_METHODS = ("VIDEO_ANALYTICS", "MANUAL_COUNT")

#: Lane vocabulary — mirrors the CHECK constraints on core.gate_lane.
LANE_TYPES = ("IN", "OUT", "REVERSIBLE")
LANE_STATES = ("OPEN", "CLOSED", "MAINTENANCE")
BOOM_STATES = ("UP", "DOWN", "UNKNOWN")

#: Terminal code carried by each gate id, for the CPP per-terminal release lanes.
GATE_TERMINAL = {
    "G-NSICT": "NSICT",
    "G-JNPCT": "JNPCT",
    "G-NSIGT": "NSIGT",
    "G-BMCT": "BMCT",
}


class GateBoardRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _exec(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).begin() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            try:
                return [dict(r) for r in res.mappings().all()]
            except Exception:  # noqa: BLE001 — non-RETURNING statement
                return []

    # ------------------------------------------------------------------ gates
    async def gates(self) -> list[dict]:
        return await self._rows(
            "SELECT id, name, lat, lon, closed_at FROM core.gate ORDER BY id")

    async def queue_at_gates(self) -> list[dict]:
        """Latest COUNTED queue per gate, from video analytics only.

        DISTINCT ON gives the most recent observation per gate. ``count_method``
        travels with the number so the UI can state how the queue was obtained
        rather than assert that it was counted.

        Deliberately touches no throughput table: see the module docstring.
        """
        return await self._rows(
            """
            SELECT DISTINCT ON (gate_id)
                   gate_id,
                   camera_id,
                   queue_count,
                   vehicle_count,
                   congestion_level,
                   confidence,
                   count_method,
                   source,
                   ts AS observed_at
              FROM core.camera_ai_count
             WHERE gate_id IS NOT NULL
               AND count_method = ANY(:methods)
             ORDER BY gate_id, ts DESC
            """,
            {"methods": list(ACCEPTED_QUEUE_METHODS)},
        )

    async def throughput_at_gates(self, window_minutes: int = 60) -> list[dict]:
        """In/out counts and mean transaction time per gate over a window.

        Transaction time is GATE_TXN_START -> GATE_IN for the same trip. Nothing
        here is used to derive a queue length.
        """
        return await self._rows(
            """
            WITH win AS (
                SELECT * FROM core.gate_event
                 WHERE ts > now() - make_interval(mins => :mins)
                   AND gate_id IS NOT NULL
            ),
            counts AS (
                SELECT gate_id,
                       count(*) FILTER (WHERE event_type = 'GATE_IN')  AS in_count,
                       count(*) FILTER (WHERE event_type = 'GATE_OUT') AS out_count
                  FROM win GROUP BY gate_id
            ),
            txn AS (
                SELECT s.gate_id,
                       avg(EXTRACT(EPOCH FROM (i.ts - s.ts))) AS avg_txn_seconds,
                       count(*)                                AS txn_samples
                  FROM win s
                  JOIN win i
                    ON i.trip_id = s.trip_id
                   AND i.event_type = 'GATE_IN'
                   AND i.ts >= s.ts
                 WHERE s.event_type = 'GATE_TXN_START'
                 GROUP BY s.gate_id
            )
            SELECT g.id AS gate_id,
                   COALESCE(c.in_count, 0)  AS in_count,
                   COALESCE(c.out_count, 0) AS out_count,
                   t.avg_txn_seconds,
                   COALESCE(t.txn_samples, 0) AS txn_samples
              FROM core.gate g
              LEFT JOIN counts c ON c.gate_id = g.id
              LEFT JOIN txn    t ON t.gate_id = g.id
             ORDER BY g.id
            """,
            {"mins": window_minutes},
        )

    # ------------------------------------------------------------------ lanes
    async def lanes(self, gate_id: Optional[str] = None) -> list[dict]:
        where, params = "", {}
        if gate_id:
            where, params = " WHERE gate_id = :gid", {"gid": gate_id}
        return await self._rows(
            "SELECT lane_id, gate_id, lane_no, lane_type, lane_state, boom_barrier, "
            f"updated_at FROM core.gate_lane{where} ORDER BY gate_id, lane_no", params)

    async def lane(self, lane_id: str) -> Optional[dict]:
        rows = await self._rows(
            "SELECT lane_id, gate_id, lane_no, lane_type, lane_state, boom_barrier, "
            "updated_at FROM core.gate_lane WHERE lane_id = :lid", {"lid": lane_id})
        return rows[0] if rows else None

    # ------------------------------------------------------- confirmation ticker
    async def confirmations(self, limit: int = 25) -> list[dict]:
        """Recent confirmed vehicle transactions for the board ticker."""
        return await self._rows(
            """
            SELECT id, ts, gate_id, plate, device_id, trip_id, event_type,
                   container_number, bat_lane, source
              FROM core.gate_event
             WHERE event_type IN ('GATE_TXN_START', 'GATE_IN', 'GATE_OUT')
               AND gate_id IS NOT NULL
             ORDER BY ts DESC
             LIMIT :limit
            """,
            {"limit": limit},
        )

    # ------------------------------------------------- lane reassignment tasks
    async def create_reassignment_task(
        self, *, gate_id: str, lane_id: str, from_type: str, to_type: str,
        reason: Optional[str], impact_preview: Mapping[str, Any],
        created_by: Optional[str],
    ) -> dict:
        """Record the human task. dispatched_to_equipment is never passed: the
        column's CHECK pins it to false, so no caller can turn this into a
        device command (UI-103)."""
        rows = await self._exec(
            """
            INSERT INTO core.lane_reassignment_task
                   (task_id, gate_id, lane_id, from_lane_type, to_lane_type,
                    reason, impact_preview, created_by)
            VALUES (:tid, :gid, :lid, :ft, :tt, :reason,
                    CAST(:impact AS jsonb), :by)
            RETURNING task_id, gate_id, lane_id, from_lane_type, to_lane_type,
                      reason, impact_preview, status, assigned_to, created_by,
                      created_at, acknowledged_by, acknowledged_at,
                      dispatched_to_equipment
            """,
            {"tid": str(uuid.uuid4()), "gid": gate_id, "lid": lane_id,
             "ft": from_type, "tt": to_type, "reason": reason,
             "impact": json.dumps(dict(impact_preview or {})), "by": created_by},
        )
        return rows[0]

    async def reassignment_tasks(self, *, status: Optional[str] = None,
                                 limit: int = 50) -> list[dict]:
        where, params = "", {"limit": limit}
        if status:
            where, params = " WHERE status = :st", {"limit": limit, "st": status}
        return await self._rows(
            "SELECT task_id, gate_id, lane_id, from_lane_type, to_lane_type, reason, "
            "impact_preview, status, assigned_to, created_by, created_at, "
            "acknowledged_by, acknowledged_at, dispatched_to_equipment "
            f"FROM core.lane_reassignment_task{where} "
            "ORDER BY created_at DESC LIMIT :limit", params)

    async def acknowledge_task(self, task_id: str, actor: Optional[str]) -> Optional[dict]:
        rows = await self._exec(
            """
            UPDATE core.lane_reassignment_task
               SET status = 'ACKNOWLEDGED', acknowledged_by = :by,
                   acknowledged_at = now()
             WHERE task_id = CAST(:tid AS uuid) AND status = 'PENDING'
            RETURNING task_id, gate_id, lane_id, from_lane_type, to_lane_type,
                      reason, impact_preview, status, assigned_to, created_by,
                      created_at, acknowledged_by, acknowledged_at,
                      dispatched_to_equipment
            """,
            {"tid": task_id, "by": actor},
        )
        return rows[0] if rows else None

    # -------------------------------------------------------- CPP (UC3-027)
    async def cpp_occupancy(self) -> list[dict]:
        """Per-facility occupancy from REAL slot state (never a fabricated curve)."""
        return await self._rows(
            """
            SELECT f.id                AS facility_id,
                   f.facility_name,
                   f.location,
                   f.capacity,
                   f.status,
                   count(s.*) FILTER (WHERE s.availability_status = 'OCCUPIED') AS occupied
              FROM core.parking_facility f
              LEFT JOIN core.parking_slot s ON s.facility_id = f.id
             GROUP BY f.id, f.facility_name, f.location, f.capacity, f.status
             ORDER BY f.id
            """
        )

    async def cpp_dwell_histogram(self, buckets: Sequence[int] = (30, 60, 120, 240)) -> list[dict]:
        """Dwell distribution of completed parking transactions.

        Returns [] when the table is unseeded — the board then shows an explicit
        "no dwell data" state rather than an invented distribution.
        """
        return await self._rows(
            """
            SELECT width_bucket(
                       EXTRACT(EPOCH FROM (exit_time - entry_time)) / 60.0,
                       0, :top, :nbuckets) AS bucket,
                   count(*)                AS trucks,
                   min(EXTRACT(EPOCH FROM (exit_time - entry_time)) / 60.0) AS min_minutes,
                   max(EXTRACT(EPOCH FROM (exit_time - entry_time)) / 60.0) AS max_minutes
              FROM core.parking_transaction
             WHERE exit_time IS NOT NULL AND entry_time IS NOT NULL
               AND exit_time >= entry_time
             GROUP BY 1 ORDER BY 1
            """,
            {"top": float(buckets[-1]), "nbuckets": len(buckets)},
        )

    async def record_release_plans(self, plans: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Persist one recompute (all terminals, one mode) as an audit trail."""
        out: list[dict] = []
        for p in plans:
            rows = await self._exec(
                """
                INSERT INTO core.cpp_release_plan
                       (terminal_code, gate_id, gate_queue_vehicles, clearing_rate_vph,
                        release_rate_vph, hold_minutes, congestion_level, advice_text, mode)
                VALUES (:terminal, :gate, :queue, :clearing, :release, :hold,
                        :level, :advice, :mode)
                RETURNING plan_id, computed_at, terminal_code, gate_id,
                          gate_queue_vehicles, clearing_rate_vph, release_rate_vph,
                          hold_minutes, congestion_level, advice_text, mode, simulated
                """,
                {"terminal": p["terminal_code"], "gate": p.get("gate_id"),
                 "queue": p["gate_queue_vehicles"], "clearing": p["clearing_rate_vph"],
                 "release": p["release_rate_vph"], "hold": p["hold_minutes"],
                 "level": p["congestion_level"], "advice": p["advice_text"],
                 "mode": p.get("mode", "METERED")},
            )
            if rows:
                out.append(rows[0])
        return out

    async def latest_release_plans(self, mode: str = "METERED") -> list[dict]:
        return await self._rows(
            """
            SELECT DISTINCT ON (terminal_code)
                   plan_id, computed_at, terminal_code, gate_id, gate_queue_vehicles,
                   clearing_rate_vph, release_rate_vph, hold_minutes, congestion_level,
                   advice_text, mode, simulated
              FROM core.cpp_release_plan
             WHERE mode = :mode
             ORDER BY terminal_code, computed_at DESC
            """,
            {"mode": mode},
        )
