"""Data Quality ledger persistence — raw-SQL reads over ``core.dq_issue``.

Read-only by design: the ledger is written by the corpus importers (each one
owns and refreshes its own rows), so nothing here inserts, updates or deletes.
Filter COLUMNS come from a fixed whitelist and every VALUE is bound, exactly as
in :mod:`services.cfs_ecy.repository`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.dq.repository")

_COLS = ("issue_id", "file_id", "source_table", "record_ref", "issue_type",
         "severity", "description", "detected_at")
_SELECT = ", ".join(f"d.{c}" for c in _COLS)

# Severity ordering for the default sort: the worst finding first.
_SEVERITY_RANK = ("CASE d.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 "
                  "ELSE 2 END")
_SORTS = {"detected_at": "d.detected_at", "severity": _SEVERITY_RANK,
          "issue_type": "d.issue_type", "source_table": "d.source_table",
          "issue_id": "d.issue_id"}

VALID_SEVERITIES = ("info", "warn", "error")


class DqRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    @staticmethod
    def _where(f: Mapping[str, Any]) -> tuple[str, dict]:
        conds: list[str] = []
        p: dict[str, Any] = {}
        if f.get("source_table"):
            conds.append("d.source_table = :source_table")
            p["source_table"] = str(f["source_table"]).strip()
        if f.get("issue_type"):
            conds.append("d.issue_type = :issue_type")
            p["issue_type"] = str(f["issue_type"]).strip()
        if f.get("severity"):
            conds.append("d.severity = :severity")
            p["severity"] = str(f["severity"]).strip().lower()
        if f.get("file_id") is not None:
            conds.append("d.file_id = :file_id")
            p["file_id"] = int(f["file_id"])
        if f.get("q"):
            # Free-text across the description and the record reference.
            conds.append("(d.description ILIKE :q OR d.record_ref ILIKE :q)")
            p["q"] = f"%{str(f['q']).strip()}%"
        return ((" WHERE " + " AND ".join(conds)) if conds else ""), p

    async def list_issues(self, filters: Mapping[str, Any], *, sort: str,
                          direction: str, limit: int, offset: int) -> list[dict]:
        where, p = self._where(filters)
        order = _SORTS.get(sort, _SEVERITY_RANK)
        way = "DESC" if str(direction).lower() == "desc" else "ASC"
        p.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT {_SELECT}, f.path AS source_path "
            "FROM core.dq_issue d "
            "LEFT JOIN core.ingest_file f ON f.file_id = d.file_id "
            f"{where} ORDER BY {order} {way}, d.issue_id ASC "
            "LIMIT :limit OFFSET :offset", p)

    async def count_issues(self, filters: Mapping[str, Any]) -> int:
        where, p = self._where(filters)
        rows = await self._rows(
            f"SELECT count(*) AS n FROM core.dq_issue d{where}", p)
        return int(rows[0]["n"]) if rows else 0

    async def by_severity(self, filters: Mapping[str, Any]) -> list[dict]:
        where, p = self._where(filters)
        return await self._rows(
            f"SELECT d.severity, count(*)::int AS issues FROM core.dq_issue d{where} "
            "GROUP BY d.severity", p)

    async def by_source_table(self, filters: Mapping[str, Any], *,
                              limit: int = 100) -> list[dict]:
        where, p = self._where(filters)
        p["limit"] = limit
        return await self._rows(
            "SELECT d.source_table, count(*)::int AS issues, "
            "       count(*) FILTER (WHERE d.severity = 'error')::int AS errors, "
            "       count(*) FILTER (WHERE d.severity = 'warn')::int  AS warnings, "
            "       count(*) FILTER (WHERE d.severity = 'info')::int  AS info, "
            "       max(d.detected_at) AS last_seen "
            f"FROM core.dq_issue d{where} "
            "GROUP BY d.source_table ORDER BY 2 DESC, 1 LIMIT :limit", p)

    async def by_issue_type(self, filters: Mapping[str, Any], *,
                            limit: int = 100) -> list[dict]:
        where, p = self._where(filters)
        p["limit"] = limit
        return await self._rows(
            "SELECT d.issue_type, d.severity, count(*)::int AS issues, "
            "       min(d.detected_at) AS first_seen, max(d.detected_at) AS last_seen "
            f"FROM core.dq_issue d{where} "
            "GROUP BY d.issue_type, d.severity ORDER BY 3 DESC, 1 LIMIT :limit", p)
