"""ECY→CFS chain persistence — raw-SQL repository (migration 0114).

Materialises the F-Y1 empty-repositioning chain from the EXISTING flat
core.cfs_ecy_movement rows into core.ecy_cfs_chain, with per-container leg
timestamps, durations and anomaly flags.

The rebuild is ONE idempotent statement set inside one transaction:
    aggregate movements -> derive legs + durations -> classify -> upsert
so running it twice changes nothing, and it never mutates the movement rows.

Anomaly codes (the audit's silent-absorption gap):
    DUPLICATE_IN      more than one CFS gate-IN for the container
    MULTI_OUT         more than one CFS gate-OUT (the planted COSU4663595 case)
    OUT_BEFORE_IN     a CFS OUT earlier than the first CFS IN
    ORPHAN_CFS_IN     CFS activity with no preceding ECY gate-OUT
    NO_CFS_IN         an ECY gate-OUT that never arrived at a CFS
    LONG_TRANSIT      road leg > 24 h (ECY-out -> CFS-in)
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cfs_ecy.chain_repository")

# Transit longer than this is flagged (the corpus's real shuttles are 4-6 h).
LONG_TRANSIT_HOURS = 24

_REBUILD = f"""
WITH agg AS (
    SELECT
        container_number,
        min(event_ts) FILTER (WHERE facility_type = 'ECY' AND mode = 'OUT') AS ecy_out_ts,
        min(event_ts) FILTER (WHERE facility_type = 'ECY' AND mode = 'IN')  AS ecy_in_ts,
        min(event_ts) FILTER (WHERE facility_type = 'CFS' AND mode = 'IN')  AS cfs_in_ts,
        max(event_ts) FILTER (WHERE facility_type = 'CFS' AND mode = 'OUT') AS cfs_out_ts,
        min(event_ts) FILTER (WHERE facility_type = 'CFS' AND mode = 'OUT') AS cfs_first_out_ts,
        count(*) FILTER (WHERE facility_type = 'CFS' AND mode = 'IN')       AS cfs_in_count,
        count(*) FILTER (WHERE facility_type = 'CFS' AND mode = 'OUT')      AS cfs_out_count,
        count(*) FILTER (WHERE facility_type = 'ECY' AND mode = 'OUT')      AS ecy_out_count,
        count(*)                                                            AS event_count,
        min(event_ts)                                                       AS first_event_ts,
        max(event_ts)                                                       AS last_event_ts
    FROM core.cfs_ecy_movement
    GROUP BY container_number
),
calc AS (
    SELECT a.*,
        CASE WHEN ecy_out_ts IS NOT NULL AND cfs_in_ts IS NOT NULL AND cfs_in_ts >= ecy_out_ts
             THEN round(extract(epoch FROM (cfs_in_ts - ecy_out_ts)) / 3600.0::numeric, 2) END
             AS transit_hours,
        CASE WHEN cfs_in_ts IS NOT NULL AND cfs_out_ts IS NOT NULL AND cfs_out_ts >= cfs_in_ts
             THEN round(extract(epoch FROM (cfs_out_ts - cfs_in_ts)) / 3600.0::numeric, 2) END
             AS dwell_hours,
        CASE WHEN ecy_out_ts IS NOT NULL AND cfs_out_ts IS NOT NULL AND cfs_out_ts >= ecy_out_ts
             THEN round(extract(epoch FROM (cfs_out_ts - ecy_out_ts)) / 3600.0::numeric, 2) END
             AS cycle_hours,
        ((ecy_out_ts IS NOT NULL)::int + (cfs_in_ts IS NOT NULL)::int
         + (cfs_out_ts IS NOT NULL)::int) AS legs_present
    FROM agg a
),
flagged AS (
    SELECT c.*,
        ARRAY_REMOVE(ARRAY[
            CASE WHEN cfs_in_count  > 1 THEN 'DUPLICATE_IN'  END,
            CASE WHEN cfs_out_count > 1 THEN 'MULTI_OUT'     END,
            CASE WHEN cfs_first_out_ts IS NOT NULL AND cfs_in_ts IS NOT NULL
                      AND cfs_first_out_ts < cfs_in_ts THEN 'OUT_BEFORE_IN' END,
            CASE WHEN ecy_out_ts IS NULL AND cfs_in_ts IS NOT NULL THEN 'ORPHAN_CFS_IN' END,
            CASE WHEN ecy_out_ts IS NOT NULL AND cfs_in_ts IS NULL THEN 'NO_CFS_IN' END,
            CASE WHEN transit_hours > {LONG_TRANSIT_HOURS} THEN 'LONG_TRANSIT' END
        ], NULL) AS anomaly_codes
    FROM calc c
)
INSERT INTO core.ecy_cfs_chain
    (container_number, ecy_out_ts, cfs_in_ts, cfs_out_ts, ecy_in_ts,
     transit_hours, dwell_hours, cycle_hours, chain_status, legs_present,
     event_count, has_anomaly, anomaly_codes, anomaly_detail,
     first_event_ts, last_event_ts, rebuilt_at)
SELECT
    container_number, ecy_out_ts, cfs_in_ts, cfs_out_ts, ecy_in_ts,
    transit_hours, dwell_hours, cycle_hours,
    CASE WHEN legs_present = 3 THEN 'COMPLETE'
         WHEN legs_present = 0 THEN 'ORPHAN'
         ELSE 'PARTIAL' END,
    legs_present, event_count,
    (cardinality(anomaly_codes) > 0),
    anomaly_codes,
    jsonb_build_object('cfs_in_count', cfs_in_count, 'cfs_out_count', cfs_out_count,
                       'ecy_out_count', ecy_out_count,
                       'cfs_first_out_ts', cfs_first_out_ts),
    first_event_ts, last_event_ts, now()
FROM flagged
ON CONFLICT (container_number) DO UPDATE SET
    ecy_out_ts = EXCLUDED.ecy_out_ts, cfs_in_ts = EXCLUDED.cfs_in_ts,
    cfs_out_ts = EXCLUDED.cfs_out_ts, ecy_in_ts = EXCLUDED.ecy_in_ts,
    transit_hours = EXCLUDED.transit_hours, dwell_hours = EXCLUDED.dwell_hours,
    cycle_hours = EXCLUDED.cycle_hours, chain_status = EXCLUDED.chain_status,
    legs_present = EXCLUDED.legs_present, event_count = EXCLUDED.event_count,
    has_anomaly = EXCLUDED.has_anomaly, anomaly_codes = EXCLUDED.anomaly_codes,
    anomaly_detail = EXCLUDED.anomaly_detail,
    first_event_ts = EXCLUDED.first_event_ts, last_event_ts = EXCLUDED.last_event_ts,
    rebuilt_at = now()
"""


class EcyCfsChainRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _count(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(text(sql), dict(params or {}))).scalar() or 0)

    async def rebuild(self) -> dict:
        """Recompute every chain from the movement rows. Idempotent."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(_REBUILD))
            total = int((await conn.execute(
                text("SELECT count(*) FROM core.ecy_cfs_chain"))).scalar() or 0)
            complete = int((await conn.execute(text(
                "SELECT count(*) FROM core.ecy_cfs_chain WHERE chain_status = 'COMPLETE'"))).scalar() or 0)
            anomalies = int((await conn.execute(text(
                "SELECT count(*) FROM core.ecy_cfs_chain WHERE has_anomaly"))).scalar() or 0)
        log.info("ecy_cfs_chain.rebuilt", extra={"chains": total, "complete": complete,
                                                 "anomalies": anomalies})
        return {"chains": total, "complete": complete, "anomalies": anomalies}

    @staticmethod
    def _where(f: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, p = [], {}
        if f.get("container_number"):
            clauses.append("container_number = :cn")
            p["cn"] = str(f["container_number"]).strip().upper()
        if f.get("chain_status"):
            clauses.append("chain_status = :st")
            p["st"] = str(f["chain_status"]).upper()
        if f.get("anomaly_only"):
            clauses.append("has_anomaly IS TRUE")
        if f.get("anomaly_code"):
            clauses.append(":code = ANY(anomaly_codes)")
            p["code"] = str(f["anomaly_code"]).upper()
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), p

    async def list_chains(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, p = self._where(filters)
        p.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT * FROM core.ecy_cfs_chain{where} "
            "ORDER BY cfs_out_ts DESC NULLS LAST, id DESC LIMIT :limit OFFSET :offset", p)

    async def count_chains(self, *, filters: Mapping[str, Any]) -> int:
        where, p = self._where(filters)
        return await self._count(f"SELECT count(*) FROM core.ecy_cfs_chain{where}", p)

    async def get_chain(self, container_number: str) -> Optional[dict]:
        rows = await self._rows(
            "SELECT * FROM core.ecy_cfs_chain WHERE container_number = :cn",
            {"cn": container_number.strip().upper()})
        return rows[0] if rows else None

    async def stats(self) -> dict:
        rows = await self._rows(
            """SELECT count(*) AS chains,
                      count(*) FILTER (WHERE chain_status = 'COMPLETE') AS complete_chains,
                      count(*) FILTER (WHERE chain_status = 'PARTIAL')  AS partial_chains,
                      count(*) FILTER (WHERE has_anomaly)               AS anomaly_chains,
                      round(avg(transit_hours), 2) AS avg_transit_hours,
                      round(avg(dwell_hours), 2)   AS avg_dwell_hours,
                      round(avg(cycle_hours), 2)   AS avg_cycle_hours,
                      percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_hours) AS median_cycle_hours
               FROM core.ecy_cfs_chain""")
        out = rows[0] if rows else {}
        out["by_anomaly"] = await self._rows(
            "SELECT unnest(anomaly_codes) AS code, count(*) AS chains "
            "FROM core.ecy_cfs_chain WHERE has_anomaly GROUP BY 1 ORDER BY 2 DESC")
        out["last_rebuilt_at"] = (await self._rows(
            "SELECT max(rebuilt_at) AS ts FROM core.ecy_cfs_chain") or [{}])[0].get("ts")
        return out
