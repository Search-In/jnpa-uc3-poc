"""Shipping-line persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the ``jnpa.sl_*`` / ``jnpa.shipping_lines``
tables. Mirrors :mod:`services.customs.repository`: reads on a plain ``connect()``,
writes inside a single ``engine.begin()`` transaction (auto-commit / auto-rollback),
no ORM.

Design guarantees for one import file:
  * ATOMIC — the ledger row, the shipping-line master upserts and every container /
    delivery-order row persist in ONE transaction. Any error rolls the ENTIRE file
    back (no half-lists), then a FAILED ledger row is recorded separately so the
    failure is still audited.
  * IDEMPOTENT — dedup at the CONTENT level (``sl_import_files.source_sha256``
    UNIQUE): re-importing unchanged bytes is a no-op (SKIPPED_DUPLICATE). Every child
    insert additionally uses ON CONFLICT DO NOTHING on its natural key, so a partial
    re-import never duplicates and NEVER overwrites an existing row.
  * BULK — children are written with executemany; the honest imported count is a
    before/after delta scoped to this file's id.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .parsers.common import ParsedList

log = get_logger("services.shipping_lines.repository")

# Canonical container columns (parser dict keys that map 1:1 to table columns).
_CONTAINER_COLS: tuple[str, ...] = (
    "list_type", "terminal", "container_no", "iso_code", "container_valid_iso",
    "freight_kind", "category", "gross_weight_kg", "weight_source_uom", "pol", "pod",
    "destination", "shipping_line_code", "vessel_visit", "voyage", "bill_of_lading",
    "seal_no", "reefer_status", "reefer_temp", "reefer_uom", "imdg_code", "un_number",
    "group_code", "client_code", "departure_mode", "nominated_cfs", "iec_code",
    "gst_no", "commodity_code",
)
_DO_COLS: tuple[str, ...] = (
    "document_number", "common_ref_number", "message_type", "sender_id",
    "receiving_party", "vcn", "imo_number", "call_sign", "stuff_destuff_flag",
    "shipping_agent_code", "vessel_country", "total_containers", "container_no",
    "iso_code", "container_valid_iso", "equipment_status", "cargo_type",
    "loading_port", "dest_port", "final_pod", "arrival_ts", "receipt_date",
    "delivery_mode", "gate_pass_no", "gate_pass_ts", "vehicle_no", "gate_number",
    "ca_code", "con_seal_status", "issued_ts", "raw_xml",
)


class ShippingLinesRepository:
    """Raw-SQL persistence for the shipping-line tables. Stateless apart from the DSN."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ---------------------------------------------------------------- helpers
    @staticmethod
    async def _scalar(conn: Any, sql: str, params: Mapping[str, Any]) -> int:
        res = await conn.execute(text(sql), params)
        return int(res.scalar() or 0)

    async def _rows(self, sql: str, params: Mapping[str, Any]) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(r) for r in res.mappings().all()]

    async def _one(self, sql: str, params: Mapping[str, Any]) -> Optional[dict]:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    async def _count(self, sql: str, params: Mapping[str, Any]) -> int:
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(text(sql), params)).scalar() or 0)

    # -------------------------------------------------------------------- events
    async def record_event(self, event: str, *, module: Optional[str] = None,
                           reference: Optional[str] = None,
                           container_no: Optional[str] = None,
                           payload: Optional[Mapping[str, Any]] = None) -> None:
        """Append one row to the append-only jnpa.sl_events log (same pattern as
        jnpa.customs_events). Generated ONLY from real shipping-line processing."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO jnpa.sl_events (event, module, reference, container_no, "
                     "payload) VALUES (:e, :m, :r, :c, CAST(:p AS jsonb))"),
                {"e": event, "m": module, "r": reference, "c": container_no,
                 "p": json.dumps(dict(payload or {}))})

    async def list_events(self, *, module: Optional[str] = None,
                          container_no: Optional[str] = None, event: Optional[str] = None,
                          reference: Optional[str] = None,
                          since_id: Optional[int] = None, limit: int = 100,
                          offset: int = 0) -> list[dict]:
        where, params = [], {"limit": limit, "offset": offset}
        for col, val in (("module", module), ("container_no", container_no),
                         ("event", event), ("reference", reference)):
            if val is not None:
                where.append(f"{col} = :{col}")
                params[col] = val
        if since_id is not None:
            where.append("id > :since_id")
            params["since_id"] = since_id
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return await self._rows(
            "SELECT id, event, module, reference, container_no, payload, created_at "
            f"FROM jnpa.sl_events{clause} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def find_file_by_sha(self, sha256: str) -> Optional[dict]:
        return await self._one(
            "SELECT id, list_type, terminal, source_file, import_status, record_count, "
            "imported_count, error_count, created_at FROM jnpa.sl_import_files "
            "WHERE source_sha256 = :sha", {"sha": sha256})

    # ------------------------------------------------------------------ persist
    async def persist(self, parsed: ParsedList, *, source_file: str, source_sha256: str,
                      physical_format: str, file_size: Optional[int] = None,
                      uploaded_by: Optional[str] = None, source: str = "DIRECTORY") -> dict:
        """Persist one parsed shipping-line file atomically + idempotently.

        ``uploaded_by``/``source`` attribute a UI upload in the ledger; the directory
        importer leaves them at their defaults (NULL / 'DIRECTORY')."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return {"file_id": existing["id"], "list_type": existing["list_type"],
                    "terminal": existing["terminal"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": existing["record_count"],
                    "imported_count": existing["imported_count"],
                    "error_count": existing["error_count"], "duplicate": True}

        h = parsed.header
        envelope = {
            "list_type": h["list_type"], "terminal": h["terminal"],
            "physical_format": physical_format, "source_file": source_file,
            "source_sha256": source_sha256, "file_size_bytes": file_size,
            "vessel_visit": h.get("vessel_visit"), "voyage": h.get("voyage"),
            "line_code": h.get("line_code"), "direction": h.get("direction"),
            "record_count": parsed.record_count,
            "uploaded_by": uploaded_by, "source": source,
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_FILE_INSERT), envelope)
                file_id = res.mappings().first()["id"]
                if parsed.delivery_orders:
                    imported = await self._persist_delivery_orders(conn, file_id, parsed.delivery_orders)
                else:
                    imported = await self._persist_containers(conn, file_id, parsed.containers)
                await conn.execute(
                    text("UPDATE jnpa.sl_import_files SET import_status = 'SUCCESS', "
                         "imported_count = :imp, error_count = 0, updated_at = now() "
                         "WHERE id = :id"), {"imp": imported, "id": file_id})
            return {"file_id": file_id, "list_type": h["list_type"], "terminal": h["terminal"],
                    "import_status": "SUCCESS", "record_count": parsed.record_count,
                    "imported_count": imported, "error_count": 0, "duplicate": False}
        except IntegrityError as exc:
            dup = await self.find_file_by_sha(source_sha256)
            if dup is not None:
                return {"file_id": dup["id"], "list_type": dup["list_type"],
                        "terminal": dup["terminal"], "import_status": "SKIPPED_DUPLICATE",
                        "record_count": dup["record_count"],
                        "imported_count": dup["imported_count"],
                        "error_count": dup["error_count"], "duplicate": True}
            return await self._record_failure(envelope, str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001 — record + surface as FAILED, never partial
            log.warning("shipping_lines.persist_failed", terminal=h.get("terminal"),
                        source_file=source_file, error=str(exc))
            return await self._record_failure(envelope, str(exc))

    async def _upsert_lines(self, conn: Any, codes: set[str]) -> None:
        """Upsert the shipping-line master for every distinct code in this file BEFORE
        inserting children (so the FK resolves). Never overwrites a populated name."""
        rows = [{"lc": c} for c in sorted(codes) if c]
        if rows:
            await conn.execute(
                text("INSERT INTO jnpa.shipping_lines (line_code) VALUES (:lc) "
                     "ON CONFLICT (line_code) DO UPDATE SET last_seen = now()"), rows)

    async def _persist_containers(self, conn: Any, file_id: int, containers: Sequence[dict]) -> int:
        if not containers:
            return 0
        await self._upsert_lines(conn, {c.get("shipping_line_code") for c in containers if c.get("shipping_line_code")})
        import hashlib
        rows = []
        for c in containers:
            row = {k: c.get(k) for k in _CONTAINER_COLS}
            row["import_file_id"] = file_id
            raw = json.dumps(c.get("raw") or {}, sort_keys=True, default=str)
            row["raw"] = raw
            # De-dup on the FULL source row: byte-identical rows collapse (idempotent /
            # duplicate-safe), but two rows that differ in ANY source field both persist —
            # so no source record is ever lost (e.g. one container listed under two
            # operator codes in the same list).
            row["row_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            rows.append(row)
        before = await self._scalar(
            conn, "SELECT count(*) FROM jnpa.sl_advance_containers WHERE import_file_id = :id",
            {"id": file_id})
        await conn.execute(text(_CONTAINER_INSERT), rows)
        after = await self._scalar(
            conn, "SELECT count(*) FROM jnpa.sl_advance_containers WHERE import_file_id = :id",
            {"id": file_id})
        return after - before

    async def _persist_delivery_orders(self, conn: Any, file_id: int, orders: Sequence[dict]) -> int:
        if not orders:
            return 0
        rows = []
        for o in orders:
            row = {k: o.get(k) for k in _DO_COLS}
            row["import_file_id"] = file_id
            rows.append(row)
        before = await self._scalar(
            conn, "SELECT count(*) FROM jnpa.sl_delivery_orders WHERE import_file_id = :id",
            {"id": file_id})
        await conn.execute(text(_DO_INSERT), rows)
        after = await self._scalar(
            conn, "SELECT count(*) FROM jnpa.sl_delivery_orders WHERE import_file_id = :id",
            {"id": file_id})
        return after - before

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope)
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_FILE_INSERT_FAILED), row)
                fid = res.mappings().first()["id"]
                await conn.execute(
                    text("INSERT INTO jnpa.sl_import_errors (import_file_id, record_ref, "
                         "error_code, error_detail) VALUES (:fid, NULL, 'PERSIST_FAILED', :d)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("shipping_lines.failure_record_failed", error=str(exc))
            fail_id = None
        return {"file_id": fail_id, "list_type": envelope["list_type"],
                "terminal": envelope["terminal"], "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate": False}

    # -------------------------------------------------------------------- reads
    async def summary(self) -> dict:
        files = await self._rows(
            "SELECT list_type, terminal, import_status, count(*) AS n, "
            "sum(imported_count) AS imported FROM jnpa.sl_import_files "
            "GROUP BY list_type, terminal, import_status ORDER BY list_type, terminal", {})
        by_list = await self._rows(
            "SELECT list_type, count(*) AS containers, count(DISTINCT container_no) AS distinct_containers "
            "FROM jnpa.sl_advance_containers GROUP BY list_type ORDER BY list_type", {})
        by_terminal = await self._rows(
            "SELECT terminal, list_type, count(*) AS containers FROM jnpa.sl_advance_containers "
            "GROUP BY terminal, list_type ORDER BY terminal, list_type", {})
        by_category = await self._rows(
            "SELECT category, count(*) AS n FROM jnpa.sl_advance_containers "
            "GROUP BY category ORDER BY n DESC", {})
        top_lines = await self._rows(
            "SELECT shipping_line_code AS line_code, count(*) AS containers "
            "FROM jnpa.sl_advance_containers WHERE shipping_line_code IS NOT NULL "
            "GROUP BY shipping_line_code ORDER BY containers DESC LIMIT 15", {})
        totals = await self._one(
            "SELECT (SELECT count(*) FROM jnpa.sl_import_files) AS files, "
            "(SELECT count(*) FROM jnpa.sl_advance_containers) AS advance_containers, "
            "(SELECT count(DISTINCT container_no) FROM jnpa.sl_advance_containers) AS distinct_containers, "
            "(SELECT count(*) FROM jnpa.sl_delivery_orders) AS delivery_orders, "
            "(SELECT count(*) FROM jnpa.shipping_lines) AS shipping_lines, "
            "(SELECT count(*) FROM jnpa.sl_advance_containers WHERE bill_of_lading IS NOT NULL) AS with_bl, "
            "(SELECT count(*) FROM jnpa.sl_import_files WHERE import_status = 'FAILED') AS failed_files", {})
        return {"totals": totals or {}, "files": files, "by_list_type": by_list,
                "by_terminal": by_terminal, "by_category": by_category, "top_lines": top_lines}

    @staticmethod
    def _adv_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("list_type", "terminal", "category", "freight_kind"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        if filters.get("shipping_line") is not None:
            clauses.append("shipping_line_code = :shipping_line")
            params["shipping_line"] = filters["shipping_line"]
        if filters.get("container") is not None:
            clauses.append("container_no = :container")
            params["container"] = filters["container"]
        if filters.get("bl") is not None:
            clauses.append("bill_of_lading = :bl")
            params["bl"] = filters["bl"]
        if filters.get("q") is not None:
            clauses.append("(container_no ILIKE :q OR bill_of_lading ILIKE :q "
                           "OR shipping_line_code ILIKE :q)")
            params["q"] = f"%{filters['q']}%"
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    _ADV_SELECT = (
        "SELECT id, import_file_id, list_type, terminal, container_no, iso_code, "
        "container_valid_iso, freight_kind, category, gross_weight_kg, weight_source_uom, "
        "pol, pod, destination, shipping_line_code, vessel_visit, voyage, bill_of_lading, "
        "seal_no, reefer_status, reefer_temp, imdg_code, un_number, group_code, client_code, "
        "departure_mode, nominated_cfs, iec_code, gst_no, commodity_code, created_at "
        "FROM jnpa.sl_advance_containers")

    async def list_containers(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._adv_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            f"{self._ADV_SELECT}{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_containers(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._adv_where(filters)
        return await self._count(f"SELECT count(*) FROM jnpa.sl_advance_containers{where}", params)

    async def container_view(self, container_no: str) -> dict:
        summary = await self._one(
            "SELECT * FROM jnpa.v_shipping_line_container WHERE container_no = :cn",
            {"cn": container_no})
        advance = await self._rows(
            f"{self._ADV_SELECT} WHERE container_no = :cn ORDER BY id DESC", {"cn": container_no})
        delivery = await self._rows(
            "SELECT id, common_ref_number, container_no, iso_code, equipment_status, "
            "shipping_agent_code, vcn, imo_number, loading_port, dest_port, final_pod, "
            "arrival_ts, receipt_date, delivery_mode, gate_pass_no, gate_pass_ts, vehicle_no, "
            "gate_number, issued_ts, created_at FROM jnpa.sl_delivery_orders "
            "WHERE container_no = :cn ORDER BY id DESC", {"cn": container_no})
        return {"container_no": container_no, "summary": summary,
                "advance_lists": advance, "delivery_orders": delivery}

    async def list_by_bl(self, bill_of_lading: str, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            f"{self._ADV_SELECT} WHERE bill_of_lading = :bl ORDER BY id DESC "
            "LIMIT :limit OFFSET :offset",
            {"bl": bill_of_lading, "limit": limit, "offset": offset})

    async def count_by_bl(self, bill_of_lading: str) -> int:
        return await self._count(
            "SELECT count(*) FROM jnpa.sl_advance_containers WHERE bill_of_lading = :bl",
            {"bl": bill_of_lading})

    async def get_line(self, line_code: str) -> Optional[dict]:
        return await self._one(
            "SELECT line_code, line_name, source, first_seen, last_seen, "
            "(SELECT count(*) FROM jnpa.sl_advance_containers a WHERE a.shipping_line_code = s.line_code) "
            "AS container_count FROM jnpa.shipping_lines s WHERE s.line_code = :lc",
            {"lc": line_code})

    async def list_lines(self, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT s.line_code, s.line_name, s.source, s.first_seen, s.last_seen, "
            "(SELECT count(*) FROM jnpa.sl_advance_containers a WHERE a.shipping_line_code = s.line_code) "
            "AS container_count FROM jnpa.shipping_lines s "
            "ORDER BY container_count DESC, s.line_code LIMIT :limit OFFSET :offset",
            {"limit": limit, "offset": offset})

    async def count_lines(self) -> int:
        return await self._count("SELECT count(*) FROM jnpa.shipping_lines", {})

    async def list_delivery_orders(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        clauses, params = [], {"limit": limit, "offset": offset}
        if filters.get("container") is not None:
            clauses.append("container_no = :container")
            params["container"] = filters["container"]
        if filters.get("vehicle") is not None:
            clauses.append("vehicle_no = :vehicle")
            params["vehicle"] = filters["vehicle"]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._rows(
            "SELECT id, common_ref_number, container_no, iso_code, equipment_status, "
            "shipping_agent_code, vcn, imo_number, loading_port, dest_port, final_pod, "
            "arrival_ts, receipt_date, delivery_mode, gate_pass_no, gate_pass_ts, vehicle_no, "
            f"gate_number, issued_ts, created_at FROM jnpa.sl_delivery_orders{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_delivery_orders(self, *, filters: Mapping[str, Any]) -> int:
        clauses, params = [], {}
        if filters.get("container") is not None:
            clauses.append("container_no = :container")
            params["container"] = filters["container"]
        if filters.get("vehicle") is not None:
            clauses.append("vehicle_no = :vehicle")
            params["vehicle"] = filters["vehicle"]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._count(f"SELECT count(*) FROM jnpa.sl_delivery_orders{where}", params)

    # ----------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("list_type", "terminal", "import_status", "source"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, list_type, terminal, physical_format, source_file, vessel_visit, "
            "voyage, line_code, direction, record_count, imported_count, error_count, "
            "import_status, error_detail, uploaded_by, source, created_at, updated_at "
            f"FROM jnpa.sl_import_files{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        return await self._count(f"SELECT count(*) FROM jnpa.sl_import_files{where}", params)

    async def get_file(self, file_id: int) -> Optional[dict]:
        return await self._one(
            "SELECT id, list_type, terminal, physical_format, source_file, source_sha256, "
            "file_size_bytes, vessel_visit, voyage, line_code, direction, record_count, "
            "imported_count, error_count, import_status, error_detail, uploaded_by, source, "
            "created_at, updated_at FROM jnpa.sl_import_files WHERE id = :id", {"id": file_id})

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT id, record_ref, error_code, error_detail, created_at "
            "FROM jnpa.sl_import_errors WHERE import_file_id = :id "
            "ORDER BY id LIMIT :limit OFFSET :offset",
            {"id": file_id, "limit": limit, "offset": offset})

    # --------------------------------------------------------------- upload helpers
    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        """Bulk-insert per-row validation errors for one upload into the EXISTING
        jnpa.sl_import_errors table (reused — no new table). Best-effort."""
        rows = [{"fid": file_id,
                 "ref": (f"row {e.get('row_number')}" if e.get("row_number") is not None else e.get("column_name")),
                 "code": e.get("error_code") or "INVALID",
                 "detail": (e.get("error_detail") or "")[:2000]}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO jnpa.sl_import_errors (import_file_id, record_ref, "
                     "error_code, error_detail) VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        """Flip a successful import to PARTIAL when some source rows were skipped as
        invalid (records the honest outcome; the valid rows are already persisted)."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("UPDATE jnpa.sl_import_files SET import_status = 'PARTIAL', "
                     "error_count = :n, updated_at = now() WHERE id = :id"),
                {"n": error_count, "id": file_id})

    async def record_rejected_upload(self, *, list_type: str, terminal: str,
                                     physical_format: str, source_file: str,
                                     source_sha256: str, file_size: Optional[int],
                                     uploaded_by: Optional[str], detail: str,
                                     errors: Sequence[Mapping[str, Any]]) -> Optional[int]:
        """Record a structurally-rejected upload (e.g. missing required columns) as a
        FAILED row in the ledger so it appears in upload history, with its column/row
        errors. Writes NO domain rows. De-dupes on sha256 like a real import."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return existing["id"]
        envelope = {
            "list_type": list_type, "terminal": terminal, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "vessel_visit": None, "voyage": None,
            "line_code": None, "direction": None, "record_count": 0,
            "error_detail": detail[:4000], "uploaded_by": uploaded_by, "source": "UPLOAD",
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_FILE_INSERT_FAILED), envelope)
                fid = res.mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("shipping_lines.reject_record_failed", error=str(exc))
            return None


# --------------------------------------------------------------------------- SQL
def _values(cols: Sequence[str], *, raw: bool = False) -> str:
    parts = [f":{c}" for c in cols]
    if raw:
        parts.append("CAST(:raw AS jsonb)")
    return ", ".join(parts)


_FILE_INSERT = """
INSERT INTO jnpa.sl_import_files
    (list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes,
     vessel_visit, voyage, line_code, direction, record_count, import_status,
     uploaded_by, source)
VALUES
    (:list_type, :terminal, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :vessel_visit, :voyage, :line_code, :direction, :record_count, 'PENDING',
     :uploaded_by, :source)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO jnpa.sl_import_files
    (list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes,
     vessel_visit, voyage, line_code, direction, record_count, import_status, error_detail,
     uploaded_by, source)
VALUES
    (:list_type, :terminal, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :vessel_visit, :voyage, :line_code, :direction, :record_count, 'FAILED', :error_detail,
     :uploaded_by, :source)
RETURNING id
"""

_CONTAINER_INSERT = (
    "INSERT INTO jnpa.sl_advance_containers (import_file_id, "
    + ", ".join(_CONTAINER_COLS) + ", raw, row_sha256) VALUES (:import_file_id, "
    + _values(_CONTAINER_COLS, raw=True) + ", :row_sha256) "
    "ON CONFLICT (import_file_id, row_sha256) DO NOTHING"
)

_DO_INSERT = (
    "INSERT INTO jnpa.sl_delivery_orders (import_file_id, "
    + ", ".join(_DO_COLS) + ") VALUES (:import_file_id, "
    + _values(_DO_COLS) + ") "
    "ON CONFLICT (COALESCE(common_ref_number, ''), container_no, COALESCE(gate_pass_no, '')) DO NOTHING"
)
