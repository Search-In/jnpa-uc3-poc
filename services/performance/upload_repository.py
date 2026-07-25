"""Performance upload persistence — raw-SQL repository (Module 12 sub-module).

The ONLY layer that speaks SQL for the upload lifecycle. Two responsibilities:
  * upload lifecycle tables (core.perf_upload / perf_import_logs / perf_upload_errors)
  * ATOMIC import of validated records into the EXISTING jnpa.perf_* dashboard
    tables (single engine.begin() transaction → all-or-nothing rollback), keyed on
    the migration-0028/0029 UNIQUE constraints.

Re-upload semantics (changed in 0038): conflicting rows are UPDATED, not skipped.
JNPA republishes corrected reports, and the previous ON CONFLICT DO NOTHING silently
discarded the corrections while reporting success. Now the newest upload for a given
report key wins, `(xmax = 0)` distinguishes a genuine insert from an update so the
history still reports inserted vs updated honestly, and every touched row carries
`source_file` / `upload_id` / `uploaded_at` for traceability.

Parameter-bound throughout. No ORM.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.performance.upload_repository")

# Provenance stamped on every imported row (added by migration 0038).
_AUDIT = ("source_file", "upload_id", "uploaded_at")

# record-key -> (table, unique-constraint, key columns, value columns)
# Key columns are never overwritten on conflict; value columns are.
_TABLES: dict[str, tuple[str, str, tuple[str, ...], tuple[str, ...]]] = {
    "snapshot": ("perf_daily_snapshot", "uq_perf_daily_snapshot",
                 ("report_date",), ("as_of_ts", "source_file")),
    "traffic": ("perf_daily_traffic", "uq_perf_daily_traffic",
                ("report_date", "terminal_code", "period"),
                ("vessels", "imp_teus", "exp_teus", "total_teus",
                 "rakes", "rail_dis_teus", "rail_ldg_teus", "rail_total_teus")),
    "tonnage": ("perf_daily_tonnage", "uq_perf_daily_tonnage",
                ("report_date", "category", "period"),
                ("vessels", "liquid_tonnes", "dry_bulk_tonnes", "break_bulk_tonnes",
                 "total_tonnes")),
    "status": ("perf_daily_terminal_status", "uq_perf_daily_status",
               ("report_date", "terminal_code"),
               ("icd_pendency_teus", "cfs_pendency_teus", "yard_import_teus",
                "yard_export_teus", "yard_transhipment_teus", "yard_total_teus",
                "yard_usable_capacity_teus", "yard_occupancy_pct", "gate_in_teus",
                "gate_out_teus", "gate_total_teus", "reefer_total_slots",
                "reefer_occupied_slots", "reefer_available_slots")),
    "vessels": ("perf_daily_vessels", "uq_perf_daily_vessel",
                ("report_date", "terminal_code", "berth_no", "via_no"),
                ("vessel_name", "cargo_commodity", "berthed_on", "expected_completion")),
    "monthly": ("perf_monthly_teu", "uq_perf_monthly_teu",
                ("month_date", "terminal_code"),
                ("fiscal_year", "year_label", "month_label", "vessel_calls",
                 "discharge_teus", "load_teus", "total_teus")),
    "ldb_port_dwell": ("perf_ldb_port_dwell", "uq_perf_ldb_port_dwell",
                       ("report_month", "terminal_code", "cycle", "segment"),
                       ("dwell_hours", "dwell_hours_prev")),
    "ldb_facility": ("perf_ldb_facility_dwell", "uq_perf_ldb_facility_dwell",
                     ("report_month", "facility_type", "facility_name_norm"),
                     ("facility_name", "dwell_hours", "dwell_hours_prev")),
    "ldb_congestion": ("perf_ldb_congestion", "uq_perf_ldb_congestion",
                       ("report_month", "cycle", "cluster_no"),
                       ("cluster_name", "cfs_count", "pct_containers", "congestion_level")),
    "ldb_routes": ("perf_ldb_route_movement", "uq_perf_ldb_route",
                   ("report_month", "cycle", "transport_mode", "route_name"),
                   ("pct_share",)),
    "ldb_weather": ("perf_ldb_weather", "uq_perf_ldb_weather",
                    ("report_month", "terminal_code", "cycle", "weather"),
                    ("dwell_hours",)),
}
# order matters: snapshot (parent-ish) before the daily detail tables
_ORDER = ("snapshot", "traffic", "tonnage", "status", "vessels", "monthly",
          "ldb_port_dwell", "ldb_facility", "ldb_congestion", "ldb_routes", "ldb_weather")


def _build(key: str) -> tuple[str, tuple[str, ...]]:
    """Compose the upsert for one record kind. Returns (sql, bind-column order).

    `uploaded_at` is the server clock (``now()``), not a bind, so a client cannot
    backdate provenance. RETURNING (xmax = 0) lets the caller tell an INSERT from an
    UPDATE: on a fresh insert the row has no previous version, so xmax is 0.
    """
    table, constraint, keys, vals = _TABLES[key]
    # dedupe: perf_daily_snapshot already owns a source_file column of its own
    bind = tuple(dict.fromkeys(keys + vals + ("source_file", "upload_id")))
    cols = bind + ("uploaded_at",)
    placeholders = ", ".join([f":{c}" for c in bind] + ["now()"])
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in keys)
    sql = (f"INSERT INTO core.{table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT ON CONSTRAINT {constraint} DO UPDATE SET {updates} "
           f"RETURNING (xmax = 0) AS was_insert")
    return sql, bind


_INSERTS: dict[str, tuple[str, tuple[str, ...]]] = {k: _build(k) for k in _TABLES}


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
            sql = "SELECT report_date AS k FROM core.perf_daily_snapshot WHERE report_date = ANY(:ks)"
        elif report_type == "monthly_teu":
            sql = "SELECT DISTINCT month_date AS k FROM core.perf_monthly_teu WHERE month_date = ANY(:ks)"
        else:
            sql = "SELECT DISTINCT report_month AS k FROM core.perf_ldb_port_dwell WHERE report_month = ANY(:ks)"
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), {"ks": list(keys)})).scalars().all()
        return set(rows)

    # ---------------------------------------------------------- upload history
    async def create_upload(self, *, report_type: str, filename: str, size: int, uploaded_by: str,
                            status: str, row_count: int, error_count: int, notes: str,
                            file_format: str = "CSV") -> str:
        sql = ("""INSERT INTO core.perf_upload
            (report_type, original_filename, file_format, file_size_bytes, uploaded_by,
             status, row_count, error_count, notes)
            VALUES (:rt,:fn,:ff,:sz,:ub,:st,:rc,:ec,:no) RETURNING upload_id""")
        async with get_engine(self._dsn).begin() as conn:
            uid = (await conn.execute(text(sql), {"rt": report_type, "fn": filename,
                    "ff": file_format, "sz": size, "ub": uploaded_by, "st": status,
                    "rc": row_count, "ec": error_count, "no": notes})).scalar()
        return str(uid)

    async def add_errors(self, upload_id: str, errors: list[dict]) -> None:
        if not errors:
            return
        sql = ("""INSERT INTO core.perf_upload_error
            (upload_id, row_number, column_name, error_code, error_detail, raw_value)
            VALUES (:uid,:rn,:cn,:ec,:ed,:rv)""")
        async with get_engine(self._dsn).begin() as conn:
            for e in errors:
                await conn.execute(text(sql), {"uid": upload_id, "rn": e.get("row_number"),
                    "cn": e.get("column_name"), "ec": e.get("error_code"),
                    "ed": e.get("error_detail"), "rv": e.get("raw_value")})

    async def add_log(self, upload_id: str, phase: str, level: str, message: str,
                      target_table: Optional[str] = None, affected: Optional[int] = None) -> None:
        sql = ("""INSERT INTO core.perf_import_log (upload_id, phase, level, message, target_table, affected_rows)
                  VALUES (:uid,:ph,:lv,:msg,:tt,:af)""")
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(sql), {"uid": upload_id, "ph": phase, "lv": level,
                "msg": message, "tt": target_table, "af": affected})

    # ---------------------------------------------------------- atomic import
    async def import_records(self, records: Mapping[str, list[dict]], *,
                             upload_id: Optional[str] = None,
                             source_file: Optional[str] = None) -> tuple[int, int, list[tuple]]:
        """Upsert all records in ONE transaction. Returns (inserted, updated, per_table).

        Conflicting rows are REPLACED (a re-uploaded corrected report must correct the
        data), and every row is stamped with the upload that produced it. Any error
        rolls the whole thing back (no partial import).
        """
        inserted = updated = 0
        per_table: list[tuple] = []
        async with get_engine(self._dsn).begin() as conn:   # single tx → atomic
            for key in _ORDER:
                rows = records.get(key) or []
                if not rows:
                    continue
                sql_text, cols = _INSERTS[key]
                stmt = text(sql_text)
                tbl_ins = tbl_upd = 0
                for r in rows:
                    params = {c: r.get(c) for c in cols}
                    params["upload_id"] = upload_id
                    # perf_daily_snapshot carries the report's own source_file; for every
                    # other table it is the provenance stamp. Both resolve to this file.
                    params["source_file"] = r.get("source_file") or source_file
                    res = await conn.execute(stmt, params)
                    row = res.first()
                    if row is not None and row[0]:
                        tbl_ins += 1
                    else:
                        tbl_upd += 1
                inserted += tbl_ins
                updated += tbl_upd
                per_table.append((key, len(rows), tbl_ins, tbl_upd))
        return inserted, updated, per_table

    async def finalize_upload(self, upload_id: str, *, status: str, inserted: int,
                              skipped: int, updated: int = 0,
                              notes: Optional[str] = None) -> None:
        sql = ("""UPDATE core.perf_upload
                  SET status=:st, inserted_count=:ins, skipped_count=:sk, updated_count=:up,
                      completed_at=now(), notes=COALESCE(:no, notes)
                  WHERE upload_id=:uid""")
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(sql), {"st": status, "ins": inserted, "sk": skipped,
                                           "up": updated, "no": notes, "uid": upload_id})

    # ---------------------------------------------------------- reads
    async def list_uploads(self, filters: Mapping[str, Any], *, limit: int, offset: int) -> tuple[list[dict], int]:
        conds, params = [], {}
        if filters.get("report_type"):
            conds.append("report_type = :rt"); params["rt"] = filters["report_type"]
        if filters.get("status"):
            conds.append("status = :st"); params["st"] = filters["status"]
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(f"SELECT count(*) FROM core.perf_upload {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT upload_id::text, report_type, original_filename, file_format, "
                "  file_size_bytes, status, uploaded_by, row_count, inserted_count, "
                "  updated_count, skipped_count, error_count, notes, created_at, completed_at "
                f"FROM core.perf_upload {where} ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        return [dict(r) for r in rows], int(total or 0)

    async def get_upload(self, upload_id: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            head = (await conn.execute(text(
                "SELECT upload_id::text, report_type, original_filename, file_format, status, "
                "  uploaded_by, row_count, inserted_count, updated_count, skipped_count, "
                "  error_count, notes, created_at, completed_at "
                "FROM core.perf_upload WHERE upload_id = :uid"), {"uid": upload_id})).mappings().first()
            if not head:
                return None
            logs = (await conn.execute(text(
                "SELECT phase, level, message, target_table, affected_rows, created_at "
                "FROM core.perf_import_log WHERE upload_id=:uid ORDER BY created_at"), {"uid": upload_id})).mappings().all()
            errs = (await conn.execute(text(
                "SELECT row_number, column_name, error_code, error_detail, raw_value "
                "FROM core.perf_upload_error WHERE upload_id=:uid ORDER BY row_number LIMIT 500"), {"uid": upload_id})).mappings().all()
        return {**dict(head), "logs": [dict(r) for r in logs], "errors": [dict(r) for r in errs]}
