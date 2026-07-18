"""CFS-ECY CODECO persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to ``jnpa.cfs_ecy_movements`` (+ the derived
``jnpa.v_cfs_ecy_dwell`` view). It performs no business logic and no HTTP; it just
runs parameterised statements through the cached SQLAlchemy async engine
(``jnpa_shared.db.get_engine``) exactly like :mod:`services.cargo.repository` —
reads on a plain ``connect()``. No ORM.

Read-only wrt every EXISTING table: the one cross-module read is a soft lookup of
``jnpa.cargo.lifecycle_status`` by container_number (no write, no FK), used to
enrich a container timeline with its lifecycle state when the container is also
tracked by the Container Lifecycle module.

Injection-safe by construction: filter COLUMN names are fixed identifiers from a
whitelist, never interpolated from client input; every VALUE is a bound parameter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cfs_ecy.repository")

_COLUMNS = (
    "id", "facility_type", "container_number", "iso_valid",
    "event_ts", "mode", "source", "source_file", "created_at",
)
_SELECT_COLS = ", ".join(f"m.{c}" for c in _COLUMNS)

# Whitelisted equality filters (keys fixed, values bound → injection-safe).
_EQ_FILTERS = ("facility_type", "mode")
# Whitelisted sort columns.
_SORTS = {"event_ts": "m.event_ts", "container_number": "m.container_number",
          "facility_type": "m.facility_type", "mode": "m.mode", "id": "m.id"}


class CfsEcyRepository:
    """Raw-SQL reads for ``jnpa.cfs_ecy_movements``. Stateless apart from the DSN,
    so a single instance is safe to share across requests (engine + pool cached)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------- filters
    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"m.{col} = :{col}")
                params[col] = val
        # container: case-insensitive contains search
        container = filters.get("container")
        if container:
            conds.append("m.container_number ILIKE :container")
            params["container"] = f"%{str(container).strip()}%"
        # event_ts date range
        if filters.get("ts_from") is not None:
            conds.append("m.event_ts >= :ts_from")
            params["ts_from"] = filters["ts_from"]
        if filters.get("ts_to") is not None:
            conds.append("m.event_ts <= :ts_to")
            params["ts_to"] = filters["ts_to"]
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    # ------------------------------------------------------------- list + count
    async def list_movements(self, filters: Mapping[str, Any], *, sort: str,
                             direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "m.event_ts")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_SELECT_COLS} FROM jnpa.cfs_ecy_movements m {clause} "
            f"ORDER BY {order_col} {order_dir}, m.id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        sql = f"SELECT count(*) AS n FROM jnpa.cfs_ecy_movements m {clause}"
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- stats
    async def stats(self, filters: Mapping[str, Any]) -> dict:
        """Movement totals, distinct container count, and net-in ('active') count,
        scoped by the same filters as the list. Dwell is computed separately
        (CFS-only) in :meth:`dwell_summary`."""
        clause, params = self._where(filters)
        sql = (
            "SELECT "
            "  count(*) FILTER (WHERE m.mode='IN')  AS total_in, "
            "  count(*) FILTER (WHERE m.mode='OUT') AS total_out, "
            "  count(DISTINCT m.container_number)   AS container_count, "
            "  count(*)                             AS total_events, "
            "  count(*) FILTER (WHERE m.iso_valid = false) AS iso_invalid "
            f"FROM jnpa.cfs_ecy_movements m {clause}"
        )
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
            # 'active' containers = still inside a facility (net IN > OUT).
            active_sql = (
                "SELECT count(*) AS n FROM ("
                "  SELECT m.container_number, m.facility_type "
                f"  FROM jnpa.cfs_ecy_movements m {clause} "
                "  GROUP BY m.container_number, m.facility_type "
                "  HAVING count(*) FILTER (WHERE m.mode='IN') > "
                "         count(*) FILTER (WHERE m.mode='OUT')"
                ") s"
            )
            active_row = (await conn.execute(text(active_sql), params)).mappings().first()
        r = dict(row) if row else {}
        return {
            "total_in": int(r.get("total_in") or 0),
            "total_out": int(r.get("total_out") or 0),
            "container_count": int(r.get("container_count") or 0),
            "total_events": int(r.get("total_events") or 0),
            "iso_invalid": int(r.get("iso_invalid") or 0),
            "active_containers": int(active_row["n"]) if active_row else 0,
        }

    async def dwell_summary(self, filters: Mapping[str, Any]) -> dict:
        """Average + median CFS dwell (hours) over containers that have a computed
        dwell in the view. ECY dwell is NULL by design, so it is naturally excluded.
        Filters restrict by facility/date via the underlying movement rows."""
        # Restrict the view to CFS + optional facility filter. Dwell is CFS-only.
        params: dict[str, Any] = {}
        conds = ["d.dwell_hours IS NOT NULL"]
        facility = filters.get("facility_type")
        if facility and facility != "CFS":
            # A non-CFS facility filter yields no dwell rows (ECY has none).
            return {"average_dwell_hours": None, "median_dwell_hours": None, "dwell_count": 0}
        sql = (
            "SELECT round(avg(d.dwell_hours)::numeric, 2) AS average_dwell_hours, "
            "  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d.dwell_hours)::numeric, 2) "
            "    AS median_dwell_hours, "
            "  count(*) AS dwell_count "
            "FROM jnpa.v_cfs_ecy_dwell d "
            f"WHERE {' AND '.join(conds)}"
        )
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
        r = dict(row) if row else {}
        return {
            "average_dwell_hours": float(r["average_dwell_hours"]) if r.get("average_dwell_hours") is not None else None,
            "median_dwell_hours": float(r["median_dwell_hours"]) if r.get("median_dwell_hours") is not None else None,
            "dwell_count": int(r.get("dwell_count") or 0),
        }

    async def daily_throughput(self, filters: Mapping[str, Any]) -> list[dict]:
        """Per-day IN/OUT counts (IST calendar day), oldest-first, for the trend chart."""
        clause, params = self._where(filters)
        sql = (
            "SELECT (m.event_ts AT TIME ZONE 'Asia/Kolkata')::date AS day, "
            "  count(*) FILTER (WHERE m.mode='IN')  AS in_count, "
            "  count(*) FILTER (WHERE m.mode='OUT') AS out_count "
            f"FROM jnpa.cfs_ecy_movements m {clause} "
            "GROUP BY day ORDER BY day ASC"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [{"day": str(r["day"]), "in_count": int(r["in_count"]),
                     "out_count": int(r["out_count"])}
                    for r in result.mappings().all()]

    # ------------------------------------------------------------- timeline
    async def container_events(self, container_number: str) -> list[dict]:
        """All CODECO gate events for one container, across BOTH facilities,
        chronological (oldest-first)."""
        sql = (
            f"SELECT {_SELECT_COLS} FROM jnpa.cfs_ecy_movements m "
            "WHERE m.container_number = :cn ORDER BY m.event_ts ASC, m.id ASC"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"cn": container_number})
            return [dict(r) for r in result.mappings().all()]

    async def container_dwell(self, container_number: str) -> list[dict]:
        """Per-facility dwell view rows for one container (CFS dwell only)."""
        sql = (
            "SELECT container_number, facility_type, first_in_ts, last_out_ts, "
            "  in_events, out_events, dwell_hours "
            "FROM jnpa.v_cfs_ecy_dwell WHERE container_number = :cn "
            "ORDER BY facility_type"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"cn": container_number})
            return [dict(r) for r in result.mappings().all()]

    async def cargo_lifecycle(self, container_number: str) -> Optional[dict]:
        """Soft, read-only lookup of the container in the EXISTING Container
        Lifecycle module (jnpa.cargo). Returns None when the container is not
        tracked there (the common case for pure off-dock CODECO events). Degrades
        gracefully if the cargo table is absent (returns None)."""
        sql = (
            "SELECT container_number, lifecycle_status, customs_status, "
            "  yard_block, is_released, vessel_name, vehicle_number, updated_at "
            "FROM jnpa.cargo WHERE container_number = :cn"
        )
        try:
            async with get_engine(self._dsn).connect() as conn:
                row = (await conn.execute(text(sql), {"cn": container_number})).mappings().first()
            return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001 — cargo table missing / read blip
            log.warning("cfs_ecy.cargo_lookup_failed", extra={"error": str(exc)})
            return None

    # ------------------------------------------------------------- dwell report
    async def dwell_report(self, filters: Mapping[str, Any], *, limit: int,
                           offset: int) -> tuple[list[dict], int]:
        """CFS dwell report: one row per CFS container with a computed dwell,
        longest-dwell-first. Returns (rows, total)."""
        params: dict[str, Any] = {}
        conds = ["d.facility_type = 'CFS'", "d.dwell_hours IS NOT NULL"]
        if filters.get("ts_from") is not None:
            conds.append("d.first_in_ts >= :ts_from")
            params["ts_from"] = filters["ts_from"]
        if filters.get("ts_to") is not None:
            conds.append("d.last_out_ts <= :ts_to")
            params["ts_to"] = filters["ts_to"]
        where = " AND ".join(conds)
        async with get_engine(self._dsn).connect() as conn:
            total_row = (await conn.execute(
                text(f"SELECT count(*) AS n FROM jnpa.v_cfs_ecy_dwell d WHERE {where}"),
                params)).mappings().first()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT d.container_number, d.facility_type, d.first_in_ts, "
                "  d.last_out_ts, d.in_events, d.out_events, d.dwell_hours "
                f"FROM jnpa.v_cfs_ecy_dwell d WHERE {where} "
                "ORDER BY d.dwell_hours DESC NULLS LAST, d.container_number ASC "
                "LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return [dict(r) for r in rows], (int(total_row["n"]) if total_row else 0)
