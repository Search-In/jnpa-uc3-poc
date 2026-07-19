"""Performance upload persistence — raw-SQL repository (Module 12 sub-module).

The ONLY layer that speaks SQL for the upload lifecycle. Two responsibilities:
  * upload lifecycle tables (jnpa.perf_uploads / perf_import_logs / perf_upload_errors)
  * ATOMIC import of validated records into the EXISTING jnpa.perf_* dashboard
    tables (single engine.begin() transaction → all-or-nothing rollback), reusing
    the same ON CONFLICT DO NOTHING idempotency as the PDF importer.

Parameter-bound throughout. No ORM.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.performance.upload_repository")

# Reuses the migration-0028 UNIQUE constraints for idempotency.
_INSERTS = {
    "snapshot": ("""INSERT INTO jnpa.perf_daily_snapshot (report_date, source_file)
        VALUES (:report_date, :source_file) ON CONFLICT ON CONSTRAINT uq_perf_daily_snapshot DO NOTHING""",
        ("report_date", "source_file")),
    "traffic": ("""INSERT INTO jnpa.perf_daily_traffic
        (report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus)
        VALUES (:report_date,:terminal_code,:period,:vessels,:imp_teus,:exp_teus,:total_teus)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_traffic DO NOTHING""",
        ("report_date", "terminal_code", "period", "vessels", "imp_teus", "exp_teus", "total_teus")),
    "status": ("""INSERT INTO jnpa.perf_daily_terminal_status
        (report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus,
         yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus,
         yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus,
         reefer_total_slots, reefer_occupied_slots, reefer_available_slots)
        VALUES (:report_date,:terminal_code,:icd_pendency_teus,:cfs_pendency_teus,:yard_import_teus,
         :yard_export_teus,:yard_transhipment_teus,:yard_total_teus,:yard_usable_capacity_teus,
         :yard_occupancy_pct,:gate_in_teus,:gate_out_teus,:gate_total_teus,
         :reefer_total_slots,:reefer_occupied_slots,:reefer_available_slots)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_status DO NOTHING""",
        ("report_date", "terminal_code", "icd_pendency_teus", "cfs_pendency_teus", "yard_import_teus",
         "yard_export_teus", "yard_transhipment_teus", "yard_total_teus", "yard_usable_capacity_teus",
         "yard_occupancy_pct", "gate_in_teus", "gate_out_teus", "gate_total_teus",
         "reefer_total_slots", "reefer_occupied_slots", "reefer_available_slots")),
    "monthly": ("""INSERT INTO jnpa.perf_monthly_teu
        (fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls,
         discharge_teus, load_teus, total_teus)
        VALUES (:fiscal_year,:month_date,:year_label,:month_label,:terminal_code,:vessel_calls,
         :discharge_teus,:load_teus,:total_teus)
        ON CONFLICT ON CONSTRAINT uq_perf_monthly_teu DO NOTHING""",
        ("fiscal_year", "month_date", "year_label", "month_label", "terminal_code", "vessel_calls",
         "discharge_teus", "load_teus", "total_teus")),
    "ldb_port_dwell": ("""INSERT INTO jnpa.perf_ldb_port_dwell
        (report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev)
        VALUES (:report_month,:terminal_code,:cycle,:segment,:dwell_hours,:dwell_hours_prev)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_port_dwell DO NOTHING""",
        ("report_month", "terminal_code", "cycle", "segment", "dwell_hours", "dwell_hours_prev")),
}
# order matters: snapshot (parent-ish) before traffic/status
_ORDER = ("snapshot", "traffic", "status", "monthly", "ldb_port_dwell")


class UploadRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ---------------------------------------------------------- duplicate check
    async def existing_report_keys(self, report_type: str, keys: list) -> set:
        """Which of the given report_date/month keys already have data (for the
        duplicate-report validation warning)."""
        if not keys:
            return set()
        if report_type == "daily_status":
            sql = "SELECT report_date AS k FROM jnpa.perf_daily_snapshot WHERE report_date = ANY(:ks)"
        elif report_type == "monthly_teu":
            sql = "SELECT DISTINCT month_date AS k FROM jnpa.perf_monthly_teu WHERE month_date = ANY(:ks)"
        else:
            sql = "SELECT DISTINCT report_month AS k FROM jnpa.perf_ldb_port_dwell WHERE report_month = ANY(:ks)"
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), {"ks": list(keys)})).scalars().all()
        return set(rows)

    # ---------------------------------------------------------- upload history
    async def create_upload(self, *, report_type: str, filename: str, size: int, uploaded_by: str,
                            status: str, row_count: int, error_count: int, notes: str) -> str:
        sql = ("""INSERT INTO jnpa.perf_uploads
            (report_type, original_filename, file_size_bytes, uploaded_by, status, row_count, error_count, notes)
            VALUES (:rt,:fn,:sz,:ub,:st,:rc,:ec,:no) RETURNING upload_id""")
        async with get_engine(self._dsn).begin() as conn:
            uid = (await conn.execute(text(sql), {"rt": report_type, "fn": filename, "sz": size,
                    "ub": uploaded_by, "st": status, "rc": row_count, "ec": error_count, "no": notes})).scalar()
        return str(uid)

    async def add_errors(self, upload_id: str, errors: list[dict]) -> None:
        if not errors:
            return
        sql = ("""INSERT INTO jnpa.perf_upload_errors
            (upload_id, row_number, column_name, error_code, error_detail, raw_value)
            VALUES (:uid,:rn,:cn,:ec,:ed,:rv)""")
        async with get_engine(self._dsn).begin() as conn:
            for e in errors:
                await conn.execute(text(sql), {"uid": upload_id, "rn": e.get("row_number"),
                    "cn": e.get("column_name"), "ec": e.get("error_code"),
                    "ed": e.get("error_detail"), "rv": e.get("raw_value")})

    async def add_log(self, upload_id: str, phase: str, level: str, message: str,
                      target_table: Optional[str] = None, affected: Optional[int] = None) -> None:
        sql = ("""INSERT INTO jnpa.perf_import_logs (upload_id, phase, level, message, target_table, affected_rows)
                  VALUES (:uid,:ph,:lv,:msg,:tt,:af)""")
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(sql), {"uid": upload_id, "ph": phase, "lv": level,
                "msg": message, "tt": target_table, "af": affected})

    # ---------------------------------------------------------- atomic import
    async def import_records(self, records: Mapping[str, list[dict]]) -> tuple[int, int, list[tuple]]:
        """Insert all records in ONE transaction. Returns (inserted, skipped, per_table).
        Any error rolls the whole thing back (no partial import)."""
        inserted = skipped = 0
        per_table: list[tuple] = []
        async with get_engine(self._dsn).begin() as conn:   # single tx → atomic
            for key in _ORDER:
                rows = records.get(key) or []
                if not rows:
                    continue
                sql_text, cols = _INSERTS[key]
                stmt = text(sql_text)
                tbl_ins = 0
                for r in rows:
                    params = {c: r.get(c) for c in cols}
                    res = await conn.execute(stmt, params)
                    tbl_ins += (res.rowcount or 0)
                inserted += tbl_ins
                skipped += (len(rows) - tbl_ins)
                per_table.append((key, len(rows), tbl_ins))
        return inserted, skipped, per_table

    async def finalize_upload(self, upload_id: str, *, status: str, inserted: int,
                              skipped: int, notes: Optional[str] = None) -> None:
        sql = ("""UPDATE jnpa.perf_uploads
                  SET status=:st, inserted_count=:ins, skipped_count=:sk,
                      completed_at=now(), notes=COALESCE(:no, notes)
                  WHERE upload_id=:uid""")
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(sql), {"st": status, "ins": inserted, "sk": skipped,
                                           "no": notes, "uid": upload_id})

    # ---------------------------------------------------------- reads
    async def list_uploads(self, filters: Mapping[str, Any], *, limit: int, offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("report_type"):
            conds.append("report_type = :rt"); params["rt"] = filters["report_type"]
        if filters.get("status"):
            conds.append("status = :st"); params["st"] = filters["status"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*) FROM jnpa.perf_uploads {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT upload_id::text, report_type, original_filename, file_size_bytes, status, "
                "  uploaded_by, row_count, inserted_count, skipped_count, error_count, notes, "
                "  created_at, completed_at "
                f"FROM jnpa.perf_uploads {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def get_upload(self, upload_id: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            head = (await conn.execute(text(
                "SELECT upload_id::text, report_type, original_filename, status, uploaded_by, "
                "  row_count, inserted_count, skipped_count, error_count, notes, created_at, completed_at "
                "FROM jnpa.perf_uploads WHERE upload_id = :uid"), {"uid": upload_id})).mappings().first()
            if not head:
                return None
            logs = (await conn.execute(text(
                "SELECT phase, level, message, target_table, affected_rows, created_at "
                "FROM jnpa.perf_import_logs WHERE upload_id=:uid ORDER BY created_at"), {"uid": upload_id})).mappings().all()
            errs = (await conn.execute(text(
                "SELECT row_number, column_name, error_code, error_detail, raw_value "
                "FROM jnpa.perf_upload_errors WHERE upload_id=:uid ORDER BY row_number LIMIT 500"), {"uid": upload_id})).mappings().all()
        return {**dict(head), "logs": [dict(r) for r in logs], "errors": [dict(r) for r in errs]}
