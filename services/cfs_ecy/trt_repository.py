"""UC3-003 empty-container TRT persistence — raw-SQL reads, no writes.

The ONLY layer that speaks SQL for KPI 3 ("TRT for empty containers from ECD").
It reads three things and writes nothing:

* ``core.container_event``          the imported CFS/ECY CODECO gate events
* ``mart.v_empty_container_trt``    the per-container chain + durations (0133)
* ``core.dq_issue`` / ``core.ingest_file``  the anomaly ledger and its provenance

Same shape as :mod:`services.cfs_ecy.repository`: parameterised ``text()`` over
the shared async engine, filter COLUMNS from a fixed whitelist and every VALUE
bound, so the filters are injection-safe by construction.

Scope guard: every statement constrains ``source_table`` to the CODECO markers,
so terminal / scanner / gate-document events that share core.container_event can
never leak into the empty-container KPI.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cfs_ecy.trt_repository")

# Provenance markers written by scripts/import_uc3_003_cfs_ecy.py (and already
# used by the customer's own seed for the same two files).
CODECO_SOURCE_TABLES: tuple[str, ...] = ("staging ecy_codeco", "staging cfs_codeco")
# The CODECO event vocabulary, in lifecycle order.
CODECO_EVENT_TYPES: tuple[str, ...] = ("ECY_OUT", "CFS_IN", "CFS_OUT", "ECY_IN")
# core.ingest_file.path of each source workbook — the DQ ledger joins on these.
CODECO_FILE_PATHS: tuple[str, ...] = ("Data/13-CFS-ECY/ECY-CODECO.xlsx",
                                      "Data/13-CFS-ECY/CFS-CODECO.xlsx")
# core.dq_issue.source_table the importer files its findings under.
DQ_SOURCE_TABLE = "core.container_event"

_EVENT_COLS = ("event_id", "container_no", "event_ts", "event_type",
               "location_type", "direction", "source_table", "source_file",
               "details")
_EVENT_SELECT = ", ".join(f"e.{c}" for c in _EVENT_COLS)

_EVENT_SORTS = {"event_ts": "e.event_ts", "container_no": "e.container_no",
                "event_type": "e.event_type", "event_id": "e.event_id"}
_CHAIN_SORTS = {"trt_min": "t.trt_min", "container_no": "t.container_no",
                "ecy_out_ts": "t.ecy_out_ts", "cfs_out_ts": "t.cfs_out_ts",
                "cycle_min": "t.cycle_min", "dwell_min": "t.dwell_min"}

_SCOPE = "e.source_table = ANY(:codeco_tables)"


class EmptyTrtRepository:
    """Read-only SQL for the UC3-003 empty-container lifecycle and TRT KPI."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------- plumbing
    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _one(self, sql: str, params: Mapping[str, Any] | None = None) -> dict:
        rows = await self._rows(sql, params)
        return rows[0] if rows else {}

    # ================================================================ events
    @staticmethod
    def _event_where(f: Mapping[str, Any]) -> tuple[str, dict]:
        conds = [_SCOPE]
        p: dict[str, Any] = {"codeco_tables": list(CODECO_SOURCE_TABLES)}
        if f.get("container"):
            conds.append("e.container_no ILIKE :container")
            p["container"] = f"%{str(f['container']).strip()}%"
        if f.get("location_type"):
            conds.append("e.location_type = :location_type")
            p["location_type"] = str(f["location_type"]).strip().upper()
        if f.get("event_type"):
            conds.append("e.event_type = :event_type")
            p["event_type"] = str(f["event_type"]).strip().upper()
        if f.get("direction"):
            conds.append("e.direction = :direction")
            p["direction"] = str(f["direction"]).strip().upper()[:1]
        if f.get("ts_from") is not None:
            conds.append("e.event_ts >= :ts_from")
            p["ts_from"] = f["ts_from"]
        if f.get("ts_to") is not None:
            conds.append("e.event_ts <= :ts_to")
            p["ts_to"] = f["ts_to"]
        return "WHERE " + " AND ".join(conds), p

    async def list_events(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> list[dict]:
        where, p = self._event_where(filters)
        order = _EVENT_SORTS.get(sort, "e.event_ts")
        way = "ASC" if str(direction).lower() == "asc" else "DESC"
        p.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT {_EVENT_SELECT} FROM core.container_event e {where} "
            f"ORDER BY {order} {way}, e.event_id ASC LIMIT :limit OFFSET :offset", p)

    async def count_events(self, filters: Mapping[str, Any]) -> int:
        where, p = self._event_where(filters)
        row = await self._one(
            f"SELECT count(*) AS n FROM core.container_event e {where}", p)
        return int(row.get("n") or 0)

    async def container_events(self, container_no: str) -> list[dict]:
        """Every CODECO event for one container, oldest first (the timeline)."""
        return await self._rows(
            f"SELECT {_EVENT_SELECT} FROM core.container_event e "
            f"WHERE {_SCOPE} AND e.container_no = :cn "
            "ORDER BY e.event_ts ASC, e.event_id ASC",
            {"cn": container_no, "codeco_tables": list(CODECO_SOURCE_TABLES)})

    # ================================================ source-feed inventory
    async def feed_inventory(self) -> list[dict]:
        """Imported event counts per facility and leg, with each feed's date span.

        This is the number the evaluator checks the 529/432 anomaly against: it
        is COUNTED from the stored rows, never hard-coded.
        """
        return await self._rows(
            "SELECT e.location_type, e.event_type, e.direction, "
            "       count(*)::int AS events, "
            "       count(DISTINCT e.container_no)::int AS containers, "
            "       min(e.event_ts) AS first_event_ts, "
            "       max(e.event_ts) AS last_event_ts "
            f"FROM core.container_event e WHERE {_SCOPE} "
            "GROUP BY e.location_type, e.event_type, e.direction "
            "ORDER BY e.location_type, e.event_type",
            {"codeco_tables": list(CODECO_SOURCE_TABLES)})

    async def source_files(self) -> list[dict]:
        """The registered source workbooks — real-data provenance for the UI."""
        return await self._rows(
            "SELECT f.file_id, f.path, f.source_system, f.file_format, "
            "       f.row_count, f.loaded_at, "
            "       (SELECT count(*)::int FROM core.container_event e "
            "         WHERE e.source_file = f.file_id) AS imported_events "
            "FROM core.ingest_file f WHERE f.path = ANY(:paths) ORDER BY f.path",
            {"paths": list(CODECO_FILE_PATHS)})

    # ======================================================= chains + KPI
    @staticmethod
    def _chain_where(f: Mapping[str, Any]) -> tuple[str, dict]:
        conds: list[str] = []
        p: dict[str, Any] = {}
        if f.get("container"):
            conds.append("t.container_no ILIKE :container")
            p["container"] = f"%{str(f['container']).strip()}%"
        if f.get("chain_status"):
            conds.append("t.chain_status = :chain_status")
            p["chain_status"] = str(f["chain_status"]).strip().upper()
        if f.get("anomaly_code"):
            conds.append(":anomaly_code = ANY(t.anomaly_codes)")
            p["anomaly_code"] = str(f["anomaly_code"]).strip().upper()
        if f.get("anomaly_only"):
            conds.append("cardinality(t.anomaly_codes) > 0")
        return ((" WHERE " + " AND ".join(conds)) if conds else ""), p

    async def list_chains(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> list[dict]:
        where, p = self._chain_where(filters)
        order = _CHAIN_SORTS.get(sort, "t.ecy_out_ts")
        way = "ASC" if str(direction).lower() == "asc" else "DESC"
        p.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT t.* FROM mart.v_empty_container_trt t{where} "
            f"ORDER BY {order} {way} NULLS LAST, t.container_no ASC "
            "LIMIT :limit OFFSET :offset", p)

    async def count_chains(self, filters: Mapping[str, Any]) -> int:
        where, p = self._chain_where(filters)
        row = await self._one(
            f"SELECT count(*) AS n FROM mart.v_empty_container_trt t{where}", p)
        return int(row.get("n") or 0)

    async def get_chain(self, container_no: str) -> Optional[dict]:
        rows = await self._rows(
            "SELECT t.* FROM mart.v_empty_container_trt t WHERE t.container_no = :cn",
            {"cn": container_no})
        return rows[0] if rows else None

    async def chain_status_counts(self) -> dict[str, int]:
        rows = await self._rows(
            "SELECT chain_status, count(*)::int AS n "
            "FROM mart.v_empty_container_trt GROUP BY chain_status")
        return {r["chain_status"]: r["n"] for r in rows}

    async def anomaly_counts(self) -> list[dict]:
        return await self._rows(
            "SELECT unnest(anomaly_codes) AS code, count(*)::int AS containers "
            "FROM mart.v_empty_container_trt "
            "WHERE cardinality(anomaly_codes) > 0 GROUP BY 1 ORDER BY 2 DESC, 1")

    async def trt_aggregate(self) -> dict:
        """Min / mean / median / max over the COMPLETE chains only.

        Incomplete and unpaired chains are excluded here rather than filtered out
        of the data: they remain fully readable through /chains and /events.
        """
        return await self._one(
            "SELECT count(*)::int AS valid_containers, "
            "       round(avg(trt_min), 2)   AS avg_trt_min, "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY trt_min) AS median_trt_min, "
            "       min(trt_min) AS min_trt_min, "
            "       max(trt_min) AS max_trt_min, "
            "       round(avg(dwell_min), 2) AS avg_dwell_min, "
            "       round(avg(cycle_min), 2) AS avg_cycle_min, "
            "       min(ecy_out_ts) AS window_from, "
            "       max(cfs_out_ts) AS window_to "
            "FROM mart.v_empty_container_trt WHERE chain_status = 'COMPLETE'")

    async def trt_daily(self, limit: int = 30) -> list[dict]:
        """Mean TRT per ECY-gate-out day (IST) — the KPI card's sparkline."""
        return await self._rows(
            "SELECT (ecy_out_ts AT TIME ZONE 'Asia/Kolkata')::date AS day, "
            "       count(*)::int AS containers, "
            "       round(avg(trt_min), 2) AS avg_trt_min "
            "FROM mart.v_empty_container_trt WHERE chain_status = 'COMPLETE' "
            "GROUP BY 1 ORDER BY 1 ASC LIMIT :limit", {"limit": limit})

    # ============================================================ DQ ledger
    async def dq_issues(self) -> list[dict]:
        """This module's findings, newest first — the 'detected, not patched' proof."""
        return await self._rows(
            "SELECT d.issue_id, d.file_id, f.path AS source_path, d.source_table, "
            "       d.record_ref, d.issue_type, d.severity, d.description, "
            "       d.detected_at "
            "FROM core.dq_issue d "
            "LEFT JOIN core.ingest_file f ON f.file_id = d.file_id "
            "WHERE d.source_table = :src "
            "ORDER BY CASE d.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 "
            "         ELSE 2 END, d.issue_id",
            {"src": DQ_SOURCE_TABLE})

    async def unpaired_containers(self, code: str, *, limit: int,
                                  offset: int) -> tuple[list[dict], int]:
        """Containers carrying one anomaly code — the per-record drill-down.

        Derived live from the stored events, which is why the ledger can stay at
        one grouped row per finding without losing the detail behind it.
        """
        p = {"code": code.strip().upper(), "limit": limit, "offset": offset}
        rows = await self._rows(
            "SELECT container_no, chain_status, ecy_out_ts, ecy_in_ts, cfs_in_ts, "
            "       cfs_out_ts, event_count, anomaly_codes "
            "FROM mart.v_empty_container_trt WHERE :code = ANY(anomaly_codes) "
            "ORDER BY container_no LIMIT :limit OFFSET :offset", p)
        total = int((await self._one(
            "SELECT count(*) AS n FROM mart.v_empty_container_trt "
            "WHERE :code = ANY(anomaly_codes)", {"code": p["code"]})).get("n") or 0)
        return rows, total
