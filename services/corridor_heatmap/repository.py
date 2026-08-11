"""Corridor heatmap persistence (UC3-020).

Flow comes from the FROZEN 20k-truck corridor simulation (core.sim_truck,
UC3-005); speed comes from core.traffic_snapshot where a segment has one.

REPLAY MAPPING — stated, not hidden
-----------------------------------
The simulation is frozen to the 20-26 July 2026 calibration week, and the demo
runs weeks later. A heatmap that silently showed "now" against a frozen week
would be claiming live data it does not have, so the mapping is explicit: the
requested instant keeps its time-of-day and weekday and is projected into the
frozen week. The projected replay instant is returned with every read, so the
caller can show WHICH moment of the replay is on screen.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.corridor_heatmap.repository")

#: The frozen calibration week the simulation was seeded over (UC3-005).
REPLAY_WINDOW_START = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
REPLAY_WINDOW_DAYS = 7


def to_replay_instant(at: dt.datetime) -> dt.datetime:
    """Project a real instant onto the frozen replay week, keeping weekday+time.

    Preserving the weekday matters: a Friday evening peak must land on the
    simulation's Friday evening, or the heatmap would show a Tuesday lull for a
    Friday demo.
    """
    if at.tzinfo is None:
        at = at.replace(tzinfo=dt.timezone.utc)
    day = REPLAY_WINDOW_START + dt.timedelta(days=at.weekday() % REPLAY_WINDOW_DAYS)
    return day.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)


class CorridorHeatmapRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def segment_flow(self, at: dt.datetime, bucket_minutes: int) -> list[dict]:
        """Per-segment flow (vehicles/hour) and speed for the bucket containing ``at``.

        Flow is the count of simulated trucks on the segment in the bucket,
        scaled to an hourly rate. Speed prefers a real traffic snapshot for the
        segment and falls back to NULL — the service then uses free-flow and says
        so, rather than this layer inventing a speed.
        """
        replay_at = to_replay_instant(at)
        try:
            return await self._rows(
                """
                -- CAST(... AS timestamptz) rather than the ::timestamptz shorthand:
                -- SQLAlchemy's text() parser reads ":replay_at::timestamptz" as a
                -- bind parameter named "replay_at:" and emits invalid SQL.
                WITH bucket AS (
                    SELECT CAST(:replay_at AS timestamptz) AS lo,
                           CAST(:replay_at AS timestamptz)
                             + make_interval(mins => :mins) AS hi
                ),
                flow AS (
                    SELECT t.segment_code,
                           count(*) AS trucks
                      FROM core.sim_truck t, bucket b
                     WHERE t.replay_ts >= b.lo AND t.replay_ts < b.hi
                     GROUP BY t.segment_code
                ),
                speed AS (
                    SELECT DISTINCT ON (segment_id)
                           segment_id, speed_kmh
                      FROM core.traffic_snapshot
                     ORDER BY segment_id, ts DESC
                )
                SELECT f.segment_code,
                       f.trucks,
                       (f.trucks * (60.0 / :mins))::numeric AS flow_vph,
                       s.speed_kmh AS speed_kph
                  FROM flow f
                  LEFT JOIN speed s ON s.segment_id = f.segment_code
                 ORDER BY f.segment_code
                """,
                {"replay_at": replay_at, "mins": bucket_minutes},
            )
        except Exception as exc:  # noqa: BLE001 — an unseeded sim is not a 500
            # WARNING, not DEBUG: a swallowed query error looks exactly like
            # "no traffic on the corridor", and those are different facts.
            log.warning("segment_flow_failed", extra={"error": str(exc)})
            return []
