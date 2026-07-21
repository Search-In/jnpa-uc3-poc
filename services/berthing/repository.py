"""Berthing Reports persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the jnpa.berthing_* tables. No ORM; parameterised
``text()`` over the cached SQLAlchemy async engine (``jnpa_shared.db.get_engine``),
exactly like :mod:`services.cfs_ecy.repository`.

Import is UPSERT-on-vessel-call: one row per (terminal, voyage_number, vessel_name).
Consecutive daily snapshots / re-imports advance the lifecycle status (never regress),
fill in newly-available timestamps (COALESCE), and accrue idempotent lifecycle events
(one row per (call, milestone)). It writes ONLY jnpa.berthing_* objects — cargo /
shipping_lines / cfs_ecy are untouched (soft value-links only).

Injection-safe: filter COLUMN names are fixed whitelist identifiers; every VALUE is a
bound parameter.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.berthing.repository")

_LIFECYCLE = ("EXPECTED", "ARRIVED", "BERTH_ASSIGNED", "BERTHING_STARTED",
              "CARGO_OPERATION", "COMPLETED", "DEPARTED")
_BERTHED = ("BERTH_ASSIGNED", "BERTHING_STARTED", "CARGO_OPERATION")
_RANK = "ARRAY['EXPECTED','ARRIVED','BERTH_ASSIGNED','BERTHING_STARTED'," \
        "'CARGO_OPERATION','COMPLETED','DEPARTED']::text[]"

_COLUMNS = (
    "id", "terminal", "vessel_name", "imo_number", "voyage_number", "shipping_line",
    "berth_number", "eta", "ata", "berthing_time", "departure_time",
    "cargo_operation_start", "cargo_operation_end", "status", "source_file",
    "import_file_id", "created_at", "updated_at",
)
_SELECT_COLS = ", ".join(f"b.{c}" for c in _COLUMNS)

_EQ_FILTERS = ("terminal", "status")
_SORTS = {"eta": "b.eta", "ata": "b.ata", "vessel_name": "b.vessel_name",
          "terminal": "b.terminal", "status": "b.status",
          "updated_at": "b.updated_at", "id": "b.id"}


class BerthingRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------- filters
    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"b.{col} = :{col}")
                params[col] = val
        if filters.get("vessel"):
            conds.append("b.vessel_name ILIKE :vessel")
            params["vessel"] = f"%{str(filters['vessel']).strip()}%"
        if filters.get("voyage"):
            conds.append("b.voyage_number ILIKE :voyage")
            params["voyage"] = f"%{str(filters['voyage']).strip()}%"
        if filters.get("berthed_only"):
            conds.append("b.status = ANY(:berthed)")
            params["berthed"] = list(_BERTHED)
        if filters.get("eta_from") is not None:
            conds.append("b.eta >= :eta_from")
            params["eta_from"] = filters["eta_from"]
        if filters.get("eta_to") is not None:
            conds.append("b.eta <= :eta_to")
            params["eta_to"] = filters["eta_to"]
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    # ------------------------------------------------------------- list + count
    async def list_reports(self, filters: Mapping[str, Any], *, sort: str,
                           direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "b.updated_at")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT_COLS} FROM jnpa.berthing_reports b {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, b.id DESC "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM jnpa.berthing_reports b {clause}"), params)).scalar() or 0)

    async def get(self, report_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT_COLS} FROM jnpa.berthing_reports b WHERE b.id = :id"),
                {"id": report_id})).mappings().first()
        return dict(row) if row else None

    async def timeline(self, report_id: int) -> Optional[dict]:
        report = await self.get(report_id)
        if report is None:
            return None
        async with get_engine(self._dsn).connect() as conn:
            events = (await conn.execute(text(
                "SELECT id, event_type, event_time, created_by, created_at "
                "FROM jnpa.berthing_events WHERE berthing_id = :id "
                f"ORDER BY array_position({_RANK}, event_type), event_time NULLS LAST, id"),
                {"id": report_id})).mappings().all()
        report["events"] = [dict(e) for e in events]
        return report

    # ------------------------------------------------------------- stats
    async def stats(self, filters: Mapping[str, Any]) -> dict:
        clause, params = self._where(filters)
        sql = (
            "SELECT count(*) AS total, "
            "  count(*) FILTER (WHERE b.status='EXPECTED')  AS expected, "
            "  count(*) FILTER (WHERE b.status='ARRIVED')   AS arrived, "
            "  count(*) FILTER (WHERE b.status = ANY(:berthed)) AS berthed, "
            "  count(*) FILTER (WHERE b.status='COMPLETED') AS completed, "
            "  count(*) FILTER (WHERE b.status='DEPARTED')  AS departed, "
            "  count(DISTINCT b.terminal) AS terminals, "
            "  round((avg(extract(epoch FROM (b.departure_time - b.ata)) / 3600.0) "
            "         FILTER (WHERE b.ata IS NOT NULL AND b.departure_time IS NOT NULL "
            "                 AND b.departure_time >= b.ata))::numeric, 1) AS avg_berth_hours "
            f"FROM jnpa.berthing_reports b {clause}"
        )
        p = dict(params); p["berthed"] = list(_BERTHED)
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), p)).mappings().first()
            by_term = (await conn.execute(text(
                "SELECT b.terminal, count(*) AS n, "
                "  count(*) FILTER (WHERE b.status = ANY(:berthed)) AS berthed "
                f"FROM jnpa.berthing_reports b {clause} "
                "GROUP BY b.terminal ORDER BY b.terminal"), p)).mappings().all()
        r = dict(row) if row else {}
        return {
            "total": int(r.get("total") or 0),
            "expected": int(r.get("expected") or 0),
            "arrived": int(r.get("arrived") or 0),
            "berthed": int(r.get("berthed") or 0),
            "completed": int(r.get("completed") or 0),
            "departed": int(r.get("departed") or 0),
            "terminals": int(r.get("terminals") or 0),
            "avg_berth_hours": float(r["avg_berth_hours"]) if r.get("avg_berth_hours") is not None else None,
            "by_terminal": [{"terminal": t["terminal"], "count": int(t["n"]),
                             "berthed": int(t["berthed"])} for t in by_term],
        }

    # ================================================================ Data Upload / import
    async def find_file_by_hash(self, file_hash: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, terminal, filename, status, total_rows, success_rows, "
                "failed_rows, duplicate_rows, created_at "
                "FROM jnpa.berthing_import_files WHERE file_hash = :h"),
                {"h": file_hash})).mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _events_for(rec: Mapping[str, Any], berthing_id: int, actor: str) -> list[dict]:
        """Derive idempotent lifecycle events from a call's non-null milestones."""
        berth = rec.get("berth_number")
        milestones = [
            ("EXPECTED", rec.get("eta")),
            ("ARRIVED", rec.get("ata")),
            ("BERTH_ASSIGNED", (rec.get("berthing_time") or rec.get("ata")) if berth else None),
            ("BERTHING_STARTED", rec.get("berthing_time")),
            ("CARGO_OPERATION", rec.get("cargo_operation_start")),
            ("COMPLETED", rec.get("cargo_operation_end")),
            ("DEPARTED", rec.get("departure_time")),
        ]
        events = [{"berthing_id": berthing_id, "event_type": et, "event_time": tv,
                   "created_by": actor} for et, tv in milestones if tv is not None]
        cur = rec.get("status") or "EXPECTED"
        if cur not in {e["event_type"] for e in events}:
            events.append({"berthing_id": berthing_id, "event_type": cur,
                           "event_time": None, "created_by": actor})
        return events

    async def persist(self, records: Sequence[Mapping[str, Any]], *, terminal: Optional[str],
                      filename: str, file_hash: str, physical_format: str,
                      file_size: Optional[int] = None, uploaded_by: Optional[str] = None,
                      source: str = "UPLOAD") -> dict:
        """Upsert one file's vessel-calls atomically + idempotently. Re-uploading
        identical bytes is a no-op (SKIPPED_DUPLICATE)."""
        existing = await self.find_file_by_hash(file_hash)
        if existing is not None:
            return {"file_id": existing["id"], "status": "SKIPPED_DUPLICATE",
                    "inserted": 0, "updated": 0, "success_rows": existing["success_rows"],
                    "duplicate_file": True}

        envelope = {"filename": filename, "file_hash": file_hash, "terminal": terminal,
                    "physical_format": physical_format, "uploaded_by": uploaded_by,
                    "total_rows": len(records), "source": source}
        actor = uploaded_by or ("importer" if source == "DIRECTORY" else "uploader")
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT), envelope)).mappings().first()["id"]
                inserted = updated = 0
                for rec in records:
                    params = {k: rec.get(k) for k in (
                        "terminal", "vessel_name", "imo_number", "voyage_number",
                        "shipping_line", "berth_number", "eta", "ata", "berthing_time",
                        "departure_time", "cargo_operation_start", "cargo_operation_end",
                        "status", "source_file")}
                    params["import_file_id"] = fid
                    res = (await conn.execute(text(_REPORT_UPSERT), params)).mappings().first()
                    rid, was_insert = res["id"], bool(res["inserted"])
                    inserted += was_insert
                    updated += (not was_insert)
                    events = self._events_for(rec, rid, actor)
                    if events:
                        await conn.execute(text(_EVENT_UPSERT), events)
                success = inserted + updated
                await conn.execute(text(
                    "UPDATE jnpa.berthing_import_files SET status='SUCCESS', "
                    "success_rows=:s, failed_rows=0, updated_at=now() WHERE id=:id"),
                    {"s": success, "id": fid})
            return {"file_id": fid, "status": "SUCCESS", "inserted": inserted,
                    "updated": updated, "success_rows": success, "duplicate_file": False}
        except IntegrityError:
            dup = await self.find_file_by_hash(file_hash)
            if dup is not None:
                return {"file_id": dup["id"], "status": "SKIPPED_DUPLICATE", "inserted": 0,
                        "updated": 0, "success_rows": dup["success_rows"], "duplicate_file": True}
            return await self._record_failure(envelope, "integrity_error")
        except Exception as exc:  # noqa: BLE001
            log.warning("berthing.persist_failed", extra={"filename": filename, "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope); row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO jnpa.berthing_import_errors (import_file_id, row_number, "
                    "error_message, raw_data) VALUES (:fid, NULL, :d, NULL)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("berthing.failure_record_failed", extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "status": "FAILED", "inserted": 0, "updated": 0,
                "success_rows": 0, "duplicate_file": False}

    async def record_rejected_upload(self, *, terminal: Optional[str], physical_format: str,
                                     filename: str, file_hash: str, uploaded_by: Optional[str],
                                     detail: str, errors: Sequence[Mapping[str, Any]]) -> Optional[int]:
        existing = await self.find_file_by_hash(file_hash)
        if existing is not None:
            return existing["id"]
        envelope = {"filename": filename, "file_hash": file_hash, "terminal": terminal,
                    "physical_format": physical_format, "uploaded_by": uploaded_by,
                    "total_rows": 0, "source": "UPLOAD", "error_detail": detail[:4000]}
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), envelope)).mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("berthing.reject_record_failed", extra={"error": str(exc)})
            return None

    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        rows = [{"fid": file_id, "rn": e.get("row_number"),
                 "msg": (f"{e.get('column_name') or ''}: {e.get('error_detail') or e.get('error_code') or ''}").strip(": ")[:2000],
                 "raw": (None if e.get("raw_value") is None else str(e.get("raw_value"))[:2000])}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "INSERT INTO jnpa.berthing_import_errors (import_file_id, row_number, "
                "error_message, raw_data) VALUES (:fid, :rn, :msg, :raw)"), rows)

    async def mark_partial(self, file_id: int, *, failed_rows: int, duplicate_rows: int = 0) -> None:
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE jnpa.berthing_import_files SET status='PARTIAL', failed_rows=:f, "
                "duplicate_rows=:d, updated_at=now() WHERE id=:id"),
                {"f": failed_rows, "d": duplicate_rows, "id": file_id})

    async def set_duplicates(self, file_id: int, *, duplicate_rows: int) -> None:
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE jnpa.berthing_import_files SET duplicate_rows=:d, updated_at=now() "
                "WHERE id=:id"), {"d": duplicate_rows, "id": file_id})

    # ------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("terminal", "status", "source"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, filename, file_hash, terminal, physical_format, uploaded_by, "
                "status, total_rows, success_rows, failed_rows, duplicate_rows, source, "
                "error_detail, created_at, updated_at "
                f"FROM jnpa.berthing_import_files{where} "
                "ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
            return [dict(r) for r in res.mappings().all()]

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM jnpa.berthing_import_files{where}"), params)).scalar() or 0)

    async def get_file(self, file_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, filename, file_hash, terminal, physical_format, uploaded_by, "
                "status, total_rows, success_rows, failed_rows, duplicate_rows, source, "
                "error_detail, created_at, updated_at "
                "FROM jnpa.berthing_import_files WHERE id = :id"), {"id": file_id})).mappings().first()
        return dict(row) if row else None

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, row_number, error_message, raw_data, created_at "
                "FROM jnpa.berthing_import_errors WHERE import_file_id = :id "
                "ORDER BY id LIMIT :limit OFFSET :offset"),
                {"id": file_id, "limit": limit, "offset": offset})
            return [dict(r) for r in res.mappings().all()]


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO jnpa.berthing_import_files
    (filename, file_hash, terminal, physical_format, uploaded_by, total_rows, status, source)
VALUES
    (:filename, :file_hash, :terminal, :physical_format, :uploaded_by, :total_rows,
     'PENDING', :source)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO jnpa.berthing_import_files
    (filename, file_hash, terminal, physical_format, uploaded_by, total_rows, status,
     error_detail, source)
VALUES
    (:filename, :file_hash, :terminal, :physical_format, :uploaded_by, :total_rows,
     'FAILED', :error_detail, :source)
RETURNING id
"""

_REPORT_UPSERT = f"""
INSERT INTO jnpa.berthing_reports
    (terminal, vessel_name, imo_number, voyage_number, shipping_line, berth_number,
     eta, ata, berthing_time, departure_time, cargo_operation_start, cargo_operation_end,
     status, source_file, import_file_id)
VALUES
    (:terminal, :vessel_name, :imo_number, :voyage_number, :shipping_line, :berth_number,
     :eta, :ata, :berthing_time, :departure_time, :cargo_operation_start,
     :cargo_operation_end, :status, :source_file, :import_file_id)
ON CONFLICT ON CONSTRAINT uq_berthing_call DO UPDATE SET
    status = CASE WHEN array_position({_RANK}, EXCLUDED.status)
                   >= array_position({_RANK}, jnpa.berthing_reports.status)
                  THEN EXCLUDED.status ELSE jnpa.berthing_reports.status END,
    imo_number            = COALESCE(EXCLUDED.imo_number, jnpa.berthing_reports.imo_number),
    shipping_line         = COALESCE(EXCLUDED.shipping_line, jnpa.berthing_reports.shipping_line),
    berth_number          = COALESCE(EXCLUDED.berth_number, jnpa.berthing_reports.berth_number),
    eta                   = COALESCE(EXCLUDED.eta, jnpa.berthing_reports.eta),
    ata                   = COALESCE(EXCLUDED.ata, jnpa.berthing_reports.ata),
    berthing_time         = COALESCE(EXCLUDED.berthing_time, jnpa.berthing_reports.berthing_time),
    departure_time        = COALESCE(EXCLUDED.departure_time, jnpa.berthing_reports.departure_time),
    cargo_operation_start = COALESCE(EXCLUDED.cargo_operation_start, jnpa.berthing_reports.cargo_operation_start),
    cargo_operation_end   = COALESCE(EXCLUDED.cargo_operation_end, jnpa.berthing_reports.cargo_operation_end),
    source_file           = EXCLUDED.source_file,
    import_file_id        = EXCLUDED.import_file_id,
    updated_at            = now()
RETURNING id, (xmax = 0) AS inserted
"""

_EVENT_UPSERT = """
INSERT INTO jnpa.berthing_events (berthing_id, event_type, event_time, created_by)
VALUES (:berthing_id, :event_type, :event_time, :created_by)
ON CONFLICT ON CONSTRAINT uq_berthing_event DO UPDATE SET
    event_time = COALESCE(jnpa.berthing_events.event_time, EXCLUDED.event_time)
"""
