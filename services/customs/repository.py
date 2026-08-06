"""Customs persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the ``jnpa.customs_*`` tables. It mirrors
:mod:`services.cargo.repository`: reads on a plain ``connect()``, writes inside a
single ``engine.begin()`` transaction (auto-commit / auto-rollback), no ORM.

Design guarantees for a customs import:
  * ATOMIC per file — one whole message (envelope + every child row) persists in a
    SINGLE transaction. Any error rolls the ENTIRE file back (no half-manifests),
    then a FAILED ledger row is recorded in a separate transaction so the failure is
    still audited.
  * IDEMPOTENT — dedup at the CONTENT level (``customs_messages.source_sha256``
    UNIQUE): re-importing unchanged bytes is a no-op (SKIPPED_DUPLICATE). Every child
    insert additionally uses ON CONFLICT on its natural key, so a partial re-import
    upserts instead of duplicating.
  * BULK — children are written with executemany + parent-id maps resolved by natural
    key, so a 2 800-container IGM is a handful of statements, not thousands.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .parsers.common import ParsedMessage

log = get_logger("services.customs.repository")


class CustomsRepository:
    """Raw-SQL persistence for the customs document tables. Stateless apart from the
    DSN, so a single shared instance is safe (engine + pool are cached)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ---------------------------------------------------------------- helpers
    @staticmethod
    async def _exec_many(conn: Any, sql: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """executemany a leaf INSERT (fire-and-forget; used for non-counted children)."""
        if rows:
            await conn.execute(text(sql), list(rows))

    @staticmethod
    async def _scalar(conn: Any, sql: str, params: Mapping[str, Any]) -> int:
        res = await conn.execute(text(sql), params)
        return int(res.scalar() or 0)

    async def _bulk_counted(self, conn: Any, sql: str, rows: Sequence[Mapping[str, Any]],
                            *, count_sql: str, count_params: Mapping[str, Any]) -> int:
        """executemany a leaf INSERT and return the TRUE number of rows inserted.

        asyncpg's executemany rowcount is unreliable under ``ON CONFLICT``, so we
        measure a before/after delta on the target rows scoped to the parent id(s)
        touched this call (``count_sql``/``count_params``), inside the same
        transaction. This reports the honest imported count even when the source
        file carries duplicate natural keys (e.g. the Shipping Bill sheet lists each
        SB several times) — the duplicates collapse and are simply not counted."""
        if not rows:
            return 0
        before = await self._scalar(conn, count_sql, count_params)
        await conn.execute(text(sql), list(rows))
        after = await self._scalar(conn, count_sql, count_params)
        return after - before

    # -------------------------------------------------------------------- events
    async def record_event(self, event: str, *, module: Optional[str] = None,
                           reference: Optional[str] = None,
                           container_no: Optional[str] = None,
                           payload: Optional[Mapping[str, Any]] = None) -> None:
        """Append one row to the append-only core.customs_event log (the same
        pattern as core.cargo_event). Generated ONLY from real customs processing."""
        import json
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO core.customs_event (event, module, reference, "
                     "container_no, payload) VALUES (:e, :m, :r, :c, CAST(:p AS jsonb))"),
                {"e": event, "m": module, "r": reference, "c": container_no,
                 "p": json.dumps(dict(payload or {}))})

    async def list_events(self, *, module: Optional[str] = None,
                          container_no: Optional[str] = None, event: Optional[str] = None,
                          since_id: Optional[int] = None, data_origin: Optional[str] = None,
                          limit: int = 100, offset: int = 0) -> list[dict]:
        """Recent customs events (newest first), optionally filtered. since_id (an
        exclusive lower bound) supports a monotonic poll cursor like cargo events.
        ``data_origin`` narrows to a LIVE/DEMO provenance; None ⇒ no filter."""
        where = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for col, val in (("module", module), ("container_no", container_no), ("event", event)):
            if val is not None:
                where.append(f"{col} = :{col}")
                params[col] = val
        if since_id is not None:
            where.append("id > :since_id")
            params["since_id"] = since_id
        if data_origin is not None:
            where.append("data_origin = :data_origin")
            params["data_origin"] = data_origin
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = ("SELECT id, event, module, reference, container_no, payload, created_at "
               f"FROM core.customs_event{clause} ORDER BY id DESC LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(r) for r in res.mappings().all()]

    async def find_message_by_sha(self, sha256: str, *,
                                  data_origin: Optional[str] = None) -> Optional[dict]:
        """Return the existing ledger row for this content hash, or None.

        Dedup is PER-ORIGIN since 0120 (UNIQUE(source_sha256, data_origin)): the same
        bytes delivered by both the JNPA API ('API') and a manual dump ('MANUAL') are
        distinct rows, so a lookup narrows to the origin it is deduping against.
        ``data_origin`` None ⇒ hash-only lookup (byte-identical to the pre-0120 query)."""
        clause = " AND data_origin = :data_origin" if data_origin is not None else ""
        params: dict[str, Any] = {"sha": sha256}
        if data_origin is not None:
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(
                text("SELECT id, module, message_type, source_file, import_status, "
                     "record_count, imported_count, error_count, created_at "
                     f"FROM core.customs_message WHERE source_sha256 = :sha{clause}"),
                params)
            row = res.mappings().first()
        return dict(row) if row else None

    # -------------------------------------------------------------------- reads
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

    # Legacy filter key -> core column. Numeric core keys compare as ::text so the
    # string query params keep working exactly as before the v3 migration.
    _FILTER_COL = {
        "igm_no": "igm_no::text",
        "sb_no": "sb_no::text",
        "smtp_no": "smtp_no::text",
        "bill_of_entry_no": "be_no::text",
        "out_of_charge_no": "ooc_no",
        "destination_code": "destination_icd",
    }

    @classmethod
    def _where(cls, filters: Mapping[str, Any], allowed: Sequence[str], *,
               alias: str = "") -> tuple[str, dict]:
        """Build a WHERE clause from a whitelisted equality filter set (keys are fixed
        identifiers, values always bound — injection-safe by construction). ``alias``
        qualifies the column names (e.g. ``v.igm_no``) without touching bind params."""
        clauses, params = [], {}
        for col in allowed:
            val = filters.get(col)
            if val is not None:
                expr = cls._FILTER_COL.get(col, col)
                qualified = f"{alias}.{expr}" if alias else expr
                clauses.append(f"{qualified} = :{col}")
                params[col] = val
        # LIVE/DEMO provenance: narrow to one data_origin ('API' | 'MANUAL') when the
        # request pinned a mode. It is a cross-cutting tag every corpus table carries
        # (0121), not a domain key, so it lives outside ``allowed`` and is qualified
        # with the same alias. Absent/None ⇒ no clause ⇒ byte-identical SQL.
        data_origin = filters.get("data_origin")
        if data_origin is not None:
            prefix = f"{alias}." if alias else ""
            clauses.append(f"{prefix}data_origin = :data_origin")
            params["data_origin"] = data_origin
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    async def list_messages(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("module", "message_type", "import_status"))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, message_type, module, control_number, primary_ref, source_file, "
            "sent_ts, record_count, imported_count, error_count, import_status, error_detail, "
            f"created_at, updated_at FROM core.customs_message{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_messages(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("module", "message_type", "import_status"))
        return await self._count(f"SELECT count(*) FROM core.customs_message{where}", params)

    async def get_message(self, message_id: int) -> Optional[dict]:
        return await self._one(
            "SELECT id, message_type, module, control_number, sender_id, receiver_id, "
            "message_id_code, sent_ts, primary_ref, source_file, source_sha256, "
            "file_size_bytes, record_count, imported_count, error_count, import_status, "
            "error_detail, created_at, updated_at FROM core.customs_message WHERE id = :id",
            {"id": message_id})

    async def list_message_errors(self, message_id: int, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT id, record_ref, error_code, error_detail, created_at "
            "FROM core.customs_import_error WHERE message_id = :id "
            "ORDER BY id LIMIT :limit OFFSET :offset",
            {"id": message_id, "limit": limit, "offset": offset})

    async def list_igm(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("igm_no",), alias="v")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT v.igm_no, v.igm_date, v.customs_house AS customs_house_code, "
            "v.imo_no AS imo_code, v.vessel_code, v.voyage_no, v.vessel_type, "
            "v.master_name, v.line_code AS shipping_line_code, "
            "v.shipping_agent AS shipping_agent_code, v.port_of_arrival, "
            "v.cargo_brief AS brief_cargo_desc, "
            "v.terminal_code AS terminal_operator_code, v.lighthouse_dues, "
            "v.declared_lines AS total_no_of_lines, v.eta AS expected_arrival, "
            "v.entry_inward_ts AS entry_inward, "
            "(SELECT count(*) FROM core.igm_line l WHERE l.igm_no = v.igm_no) AS line_count, "
            "(SELECT count(*) FROM core.igm_line_container c "
            "   WHERE c.igm_no = v.igm_no) AS container_count "
            f"FROM core.igm v{where} "
            "ORDER BY v.igm_no DESC LIMIT :limit OFFSET :offset", params)

    async def count_igm(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no",))
        return await self._count(f"SELECT count(*) FROM core.igm{where}", params)

    async def list_igm_containers(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        """Container lines declared on an IGM, enriched with their parent cargo line
        (BL, importer, POL/POD, goods description). The LEFT JOIN is by the natural
        key (igm_no, line_no, subline_no) — the same by-value join the rest of the
        customs layer uses — so a container whose cargo line is absent still lists."""
        where, params = self._where(filters, ("igm_no", "container_no"), alias="c")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT c.igm_no, c.line_no, c.subline_no, c.container_no, c.seal_no, "
            "c.agent_code AS container_agent_code, c.status AS container_status, "
            "c.packages AS no_of_packages, c.weight AS container_weight, "
            "c.iso_code AS iso_size_type, c.soc_flag, "
            "l.bl_no, l.bl_date, l.pol AS port_of_loading, l.pod AS port_of_destination, "
            "l.importer_name, l.nature_of_cargo, l.cargo_movement, "
            "l.gross_weight, l.weight_unit AS unit_of_weight, l.goods_desc AS goods_description, "
            "l.selected_scan, "
            # RMS scanner assignment for this box, when a scanning-division list
            # selected it. LATERAL + LIMIT 1 so a container on two lists cannot
            # duplicate the manifest row.
            "rc.machine_type, rc.scan_location, rc.cfs_name AS scan_cfs_name "
            "FROM core.igm_line_container c "
            "LEFT JOIN core.igm_line l "
            "  ON l.igm_no = c.igm_no AND l.line_no = c.line_no "
            " AND l.subline_no = c.subline_no "
            "LEFT JOIN LATERAL ("
            "  SELECT machine_type, scan_location, cfs_name "
            "  FROM core.rms_scan_container r WHERE r.container_no = c.container_no LIMIT 1"
            ") rc ON true"
            f"{where} "
            "ORDER BY c.line_no, c.subline_no, c.container_no "
            "LIMIT :limit OFFSET :offset", params)

    async def count_igm_containers(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no", "container_no"))
        return await self._count(f"SELECT count(*) FROM core.igm_line_container{where}", params)

    async def list_ooc(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("bill_of_entry_no", "igm_no", "out_of_charge_no"), alias="o")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT o.be_no AS bill_of_entry_no, o.be_date AS bill_of_entry_date, "
            "o.document_type, o.igm_no, o.igm_line_no AS line_no, o.igm_subline_no AS subline_no, "
            "o.ooc_no AS out_of_charge_no, o.ooc_date AS out_of_charge_date, "
            "o.importer_name, o.iec_code AS ie_code, o.cha_code, "
            "o.country_of_origin, o.packages AS no_of_packages, "
            "o.quantity AS quantity_out_of_charged, o.quantity_unit AS unit_of_quantity, "
            "o.assessable_value, o.cif_value, o.duty_paid AS total_customs_duty, "
            "(SELECT count(DISTINCT c.container_no) FROM core.ooc_item c WHERE c.be_no = o.be_no) AS container_count "
            f"FROM core.bill_of_entry_ooc o{where} "
            "ORDER BY o.be_no DESC LIMIT :limit OFFSET :offset", params)

    async def count_ooc(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("bill_of_entry_no", "igm_no", "out_of_charge_no"))
        return await self._count(f"SELECT count(*) FROM core.bill_of_entry_ooc{where}", params)

    async def ooc_detail(self, be_no: str) -> dict:
        """One Bill of Entry with its out-of-charge facts, the containers it covers
        and every invoice line item.

        core.ooc_item carries BOTH the container and the invoice item on one row
        (one row per BE + container + invoice + item serial), so the container list
        is the DISTINCT projection of the same table the items come from — there is
        no separate container table to join."""
        # be_no is bigint; bind as int so asyncpg does not reject a str param.
        try:
            key = int(str(be_no).strip())
        except ValueError:
            return {"bill_of_entry_no": be_no, "ooc": None, "containers": [], "items": []}
        params = {"be": key}
        ooc = await self._one(
            "SELECT be_no AS bill_of_entry_no, be_date AS bill_of_entry_date, document_type, "
            "igm_no, igm_line_no AS line_no, igm_subline_no AS subline_no, "
            "ooc_no AS out_of_charge_no, ooc_date AS out_of_charge_date, "
            "importer_name, importer_addr AS importer_address, importer_city, pincode AS pin_code, "
            "iec_code AS ie_code, cha_code, country_of_origin, nature_of_cargo, "
            "packages AS no_of_packages, quantity AS quantity_out_of_charged, "
            "quantity_unit AS unit_of_quantity, assessable_value, cif_value, "
            "duty_paid AS total_customs_duty "
            "FROM core.bill_of_entry_ooc WHERE be_no = :be", params)
        containers = await self._rows(
            "SELECT DISTINCT container_no FROM core.ooc_item WHERE be_no = :be "
            "ORDER BY container_no", params)
        items = await self._rows(
            "SELECT container_no, invoice_no AS invoice_number, item_sr_no, "
            "item_desc AS item_description, hs_code AS hs_classification, "
            "cif_value, assessable_value "
            "FROM core.ooc_item WHERE be_no = :be "
            "ORDER BY container_no, invoice_no, item_sr_no", params)
        return {"bill_of_entry_no": be_no, "ooc": ooc,
                "containers": [r["container_no"] for r in containers], "items": items}

    async def list_smtp(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("smtp_no", "igm_no", "bond_no"), alias="s")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT s.smtp_no, s.smtp_date, s.igm_no, s.igm_date, "
            "s.destination_icd AS destination_code, s.carrier_code, s.bond_no, "
            "s.terminal_code AS terminal_operator_code, "
            "(SELECT count(*) FROM core.smtp_container l WHERE l.smtp_no = s.smtp_no) AS line_count "
            f"FROM core.smtp_permit s{where} "
            "ORDER BY s.smtp_no DESC LIMIT :limit OFFSET :offset", params)

    async def count_smtp(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("smtp_no", "igm_no", "bond_no"))
        return await self._count(f"SELECT count(*) FROM core.smtp_permit{where}", params)

    async def list_rms(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        # selected_count/any_selected are DERIVED from the selection lines rather than
        # stored on the report — a scan list with no lines is the "No container
        # selected for scanning" case, which reads as any_selected = false.
        where, params = self._where(filters, ("igm_no",), alias="r")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT r.report_id, r.igm_no, r.igm_year, r.vessel_name, r.shipping_line, "
            "r.agent_pan AS shipping_agent, r.processing_end AS processing_end_date, "
            "(SELECT count(*) FROM core.rms_scan_container c "
            "   WHERE c.report_id = r.report_id) AS selected_count, "
            "EXISTS (SELECT 1 FROM core.rms_scan_container c "
            "        WHERE c.report_id = r.report_id) AS any_selected "
            f"FROM core.rms_scan_report r{where} "
            "ORDER BY r.report_id DESC LIMIT :limit OFFSET :offset", params)

    async def count_rms(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no",))
        return await self._count(f"SELECT count(*) FROM core.rms_scan_report{where}", params)

    # The scan-list rows are keyed to their report by report_id; igm_no lives on
    # the PARENT report. The deployed RDS carries the base child-table shape
    # (report_id, sl_no, container_no, machine_type, scan_location, cfs_name,
    # goods_desc) without migration 0102's extension columns (id / igm_no /
    # iso_valid / created_at), so every query here goes through the join and
    # selects only columns present in both schema variants.
    _RMS_CONT_FROM = ("FROM core.rms_scan_container rc "
                      "JOIN core.rms_scan_report r ON r.report_id = rc.report_id")

    @staticmethod
    def _rms_container_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        # igm_no must be bound as a real int: asyncpg infers $1's type from the
        # bigint column and rejects a str regardless of any SQL-level CAST.
        clauses = ["r.igm_no = :igm_no"]
        params: dict = {"igm_no": int(str(filters["igm_no"]).strip())}
        if filters.get("machine_type"):
            clauses.append("upper(rc.machine_type) = upper(:machine_type)")
            params["machine_type"] = filters["machine_type"]
        if filters.get("scan_location"):
            clauses.append("rc.scan_location ILIKE :scan_location")
            params["scan_location"] = f"%{filters['scan_location']}%"
        if filters.get("container_no"):
            clauses.append("rc.container_no = :container_no")
            params["container_no"] = str(filters["container_no"]).strip().upper()
        if filters.get("data_origin") is not None:
            clauses.append("rc.data_origin = :data_origin")
            params["data_origin"] = filters["data_origin"]
        return " WHERE " + " AND ".join(clauses), params

    _RMS_CONT_FROM = ("FROM core.rms_scan_container rc "
                      "JOIN core.rms_scan_report r ON r.report_id = rc.report_id")

    @staticmethod
    def _rms_container_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        # igm_no must be bound as a real int: asyncpg infers $1's type from the
        # bigint column and rejects a str regardless of any SQL-level CAST.
        clauses = ["r.igm_no = :igm_no"]
        params: dict = {"igm_no": int(str(filters["igm_no"]).strip())}
        if filters.get("machine_type"):
            clauses.append("upper(rc.machine_type) = upper(:machine_type)")
            params["machine_type"] = filters["machine_type"]
        if filters.get("scan_location"):
            clauses.append("rc.scan_location ILIKE :scan_location")
            params["scan_location"] = f"%{filters['scan_location']}%"
        if filters.get("container_no"):
            clauses.append("rc.container_no = :container_no")
            params["container_no"] = filters["container_no"]
        if filters.get("data_origin") is not None:
            clauses.append("rc.data_origin = :data_origin")
            params["data_origin"] = filters["data_origin"]
        return " WHERE " + " AND ".join(clauses), params

    async def list_rms_containers(self, *, filters: Mapping[str, Any],
                                  limit: int, offset: int) -> list[dict]:
        """The selected containers of an RMS scan list (per IGM), with the scanner
        machine/location routing facts — previously only reachable per-container.

        The selection line carries only report_id, so the IGM number, vessel and
        agent come from the parent report — the same shape the source .txt list has.
        An empty result for a known IGM is the real "No container selected for
        scanning" outcome, not a missing report.

        ``scan_machine``/``goods_desc`` and ``machine_type``/``goods_description``
        are both emitted: the two names are consumed by different dashboards, and
        aliasing one column twice is free."""
        where, params = self._rms_container_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT rc.report_id, r.igm_no, r.igm_year, rc.sl_no, rc.container_no, "
            "rc.machine_type AS scan_machine, rc.machine_type, rc.scan_location, "
            "(rc.machine_type || '-' || rc.scan_location) AS machine_code, "
            "rc.cfs_name, rc.goods_desc, rc.goods_desc AS goods_description, "
            "r.vessel_name, r.shipping_line, r.agent_pan AS shipping_agent, "
            "r.processing_end AS processing_end_date "
            f"{self._RMS_CONT_FROM}{where} "
            "ORDER BY rc.sl_no NULLS LAST LIMIT :limit OFFSET :offset", params)

    async def count_rms_containers(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._rms_container_where(filters)
        return await self._count(f"SELECT count(*) {self._RMS_CONT_FROM}{where}", params)

    async def list_leo(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("sb_no",))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT sb_no, sb_date, site_id, rotation_no, leo_date "
            f"FROM core.leo{where} ORDER BY sb_no DESC LIMIT :limit OFFSET :offset", params)

    async def count_leo(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("sb_no",))
        return await self._count(f"SELECT count(*) FROM core.leo{where}", params)

    async def list_shipping_bills(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("sb_no", "site_id"))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT sb_no, sb_date, site_id "
            f"FROM core.shipping_bill{where} ORDER BY sb_no DESC LIMIT :limit OFFSET :offset", params)

    async def count_shipping_bills(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("sb_no", "site_id"))
        return await self._count(f"SELECT count(*) FROM core.shipping_bill{where}", params)

    async def container_customs(self, container_no: str, *,
                                data_origin: Optional[str] = None) -> dict:
        """The full customs view of one container: the derived status flags + every
        customs document that references it (IGM line, OOC, SMTP line, RMS selection).
        The single soft-join binding the customs layer to a box (by value).

        LIVE/DEMO: when ``data_origin`` is set, every customs fact for the box is
        narrowed to that provenance. ``mart.v_customs_container_status`` is grouped +
        UNIONed across four tables, so rather than re-shape that shared view its read
        is gated on the base manifest table's data_origin (core.igm_line_container)
        and each document sub-query is filtered on its own base table. ``data_origin``
        None ⇒ the origin clauses collapse to '' ⇒ byte-identical SQL."""
        do = data_origin

        def _o(alias: str) -> str:
            return f" AND {alias}.data_origin = :data_origin" if do is not None else ""

        p: dict[str, Any] = {"cn": container_no}
        if do is not None:
            p["data_origin"] = do
        status_sql = (
            "SELECT container_no, igm_no, declared_igm, rms_selected, ooc_cleared, smtp_bonded "
            "FROM mart.v_customs_container_status WHERE container_no = :cn")
        if do is not None:
            # Fallback (view is grouped/unioned, not directly origin-tagged): keep the
            # box only if it is manifested under the requested origin.
            status_sql += (" AND EXISTS (SELECT 1 FROM core.igm_line_container ic "
                           "WHERE ic.container_no = :cn AND ic.data_origin = :data_origin)")
        status = await self._one(status_sql, p)
        # Vessel/voyage + IGM timestamps for this box, via the same container ->
        # cargo_line -> vessel join the ICEGATE adapter uses. One box maps to one
        # cargo line -> one vessel; ORDER BY igm_no DESC LIMIT 1 picks the latest.
        vessel = await self._one(
            "SELECT v.igm_no, v.igm_date, v.imo_no AS imo_code, v.vessel_code, v.voyage_no, "
            "v.line_code AS shipping_line_code, v.terminal_code AS terminal_operator_code, "
            "v.port_of_arrival, v.eta AS expected_arrival, "
            "v.entry_inward_ts AS entry_inward "
            "FROM core.igm_line_container c "
            "JOIN core.igm v ON v.igm_no = c.igm_no "
            f"WHERE c.container_no = :cn{_o('c')} ORDER BY v.igm_no DESC LIMIT 1", p)
        igm = await self._rows(
            "SELECT igm_no, line_no, container_no, seal_no, agent_code AS container_agent_code, "
            "status AS container_status, iso_code AS iso_size_type "
            "FROM core.igm_line_container WHERE container_no = :cn"
            + (" AND data_origin = :data_origin" if do is not None else "")
            + " ORDER BY igm_no, line_no, subline_no", p)
        ooc = await self._rows(
            "SELECT DISTINCT o.be_no AS bill_of_entry_no, o.ooc_no AS out_of_charge_no, "
            "o.ooc_date AS out_of_charge_date, o.importer_name "
            "FROM core.ooc_item oc JOIN core.bill_of_entry_ooc o ON o.be_no = oc.be_no "
            f"WHERE oc.container_no = :cn{_o('oc')} ORDER BY 1", p)
        smtp = await self._rows(
            "SELECT s.smtp_no, s.bond_no, s.destination_icd AS destination_code, "
            "sl.consignee AS consignee_name "
            "FROM core.smtp_container sl JOIN core.smtp_permit s ON s.smtp_no = sl.smtp_no "
            f"WHERE sl.container_no = :cn{_o('sl')} ORDER BY s.smtp_no", p)
        # The scan-selection line carries only report_id; the IGM number lives on the
        # parent scan report, so it is joined in rather than read off the line.
        rms = await self._rows(
            "SELECT r.igm_no, c.machine_type AS scan_machine, c.scan_location, c.cfs_name "
            "FROM core.rms_scan_container c "
            "JOIN core.rms_scan_report r ON r.report_id = c.report_id "
            f"WHERE c.container_no = :cn{_o('c')} ORDER BY c.sl_no", p)
        # The message envelope that delivered this box's manifest — the drawer's
        # "Customs Message ID" (message_id_code). Soft: absent without an IGM link.
        message = None
        igm_no = (vessel or {}).get("igm_no")
        if igm_no is not None:
            message = await self._one(
                "SELECT id, message_id_code, message_type, module, sent_ts, source_file "
                "FROM core.customs_message WHERE primary_ref = :ref"
                + (" AND data_origin = :data_origin" if do is not None else "")
                + " ORDER BY id DESC LIMIT 1",
                {**p, "ref": str(igm_no)})
        return {"container_no": container_no, "status": status, "vessel": vessel,
                "igm": igm, "ooc": ooc, "smtp": smtp, "rms": rms,
                "message": message}

    async def summary(self, *, data_origin: Optional[str] = None) -> dict:
        """Dashboard counts across the customs layer (one round trip per table).

        ``data_origin`` narrows every count to a LIVE ('API') or DEMO ('MANUAL')
        corpus; None ⇒ counts span both (byte-identical to the pre-provenance query)."""
        do = data_origin
        p: dict[str, Any] = {"data_origin": do} if do is not None else {}
        and_do = " AND data_origin = :data_origin" if do is not None else ""
        where_do = " WHERE data_origin = :data_origin" if do is not None else ""
        async with get_engine(self._dsn).connect() as conn:
            async def n(sql: str) -> int:
                return int((await conn.execute(text(sql), p)).scalar() or 0)
            # distinct_containers reads the grouped mart view when unfiltered; under a
            # LIVE/DEMO filter it becomes the distinct box count across the four
            # origin-tagged base tables the view unions (the view is not origin-tagged).
            if do is None:
                distinct_containers = await n("SELECT count(*) FROM mart.v_customs_container_status")
            else:
                distinct_containers = await n(
                    "SELECT count(*) FROM ("
                    "  SELECT container_no FROM core.igm_line_container WHERE data_origin = :data_origin"
                    "  UNION SELECT container_no FROM core.ooc_item WHERE data_origin = :data_origin"
                    "  UNION SELECT container_no FROM core.smtp_container WHERE data_origin = :data_origin"
                    "  UNION SELECT container_no FROM core.rms_scan_container WHERE data_origin = :data_origin"
                    ") x")
            return {
                "messages": await n(f"SELECT count(*) FROM core.customs_message{where_do}"),
                "igm_vessels": await n(f"SELECT count(*) FROM core.igm{where_do}"),
                "igm_containers": await n(f"SELECT count(*) FROM core.igm_line_container{where_do}"),
                "ooc": await n(f"SELECT count(*) FROM core.bill_of_entry_ooc{where_do}"),
                "smtp": await n(f"SELECT count(*) FROM core.smtp_permit{where_do}"),
                "smtp_lines": await n(f"SELECT count(*) FROM core.smtp_container{where_do}"),
                "rms_scanlists": await n(f"SELECT count(*) FROM core.rms_scan_report{where_do}"),
                "rms_containers": await n(f"SELECT count(*) FROM core.rms_scan_container{where_do}"),
                "leo": await n(f"SELECT count(*) FROM core.leo{where_do}"),
                "shipping_bills": await n(f"SELECT count(*) FROM core.shipping_bill{where_do}"),
                "distinct_containers": distinct_containers,
                "failed_imports": await n(
                    "SELECT count(*) FROM core.customs_message "
                    f"WHERE import_status = 'FAILED'{and_do}"),
            }

    # ------------------------------------------------------- cargo binding (workflow)
    async def reconcile_cargo_status(self) -> dict:
        """Bind the customs document layer to the physical container lifecycle.

        For every container that exists in BOTH core.cargo AND the customs view, drive
        core.cargo.customs_status from customs facts (using ONLY the existing enum
        values, so nothing downstream breaks):
          * Out-Of-Charge issued  -> CLEARED           (import customs release)
          * RMS-selected (not yet cleared) -> UNDER_INSPECTION  (scanning hold)
        Only rows whose status actually changes are touched. Runs in ONE transaction.
        Returns the container numbers moved to each status (for event/notification
        emission by the service). NEVER creates cargo rows and never touches a
        container that customs has no fact for — purely additive to existing data."""
        cleared: list[str] = []
        inspect: list[str] = []
        async with get_engine(self._dsn).begin() as conn:
            res = await conn.execute(text(
                "UPDATE core.cargo c SET customs_status = 'CLEARED' "
                "FROM mart.v_customs_container_status v "
                "WHERE v.container_no = c.container_number "
                "  AND v.ooc_cleared IS TRUE AND c.customs_status <> 'CLEARED' "
                "RETURNING c.container_number"))
            cleared = [r[0] for r in res.fetchall()]
            res = await conn.execute(text(
                "UPDATE core.cargo c SET customs_status = 'UNDER_INSPECTION' "
                "FROM mart.v_customs_container_status v "
                "WHERE v.container_no = c.container_number "
                "  AND v.rms_selected IS TRUE AND v.ooc_cleared IS NOT TRUE "
                "  AND c.customs_status NOT IN ('CLEARED', 'UNDER_INSPECTION') "
                "RETURNING c.container_number"))
            inspect = [r[0] for r in res.fetchall()]
        return {"cleared": cleared, "under_inspection": inspect}

    async def materialize_cargo_from_igm(
        self, *, igm_no: Optional[str] = None, limit: int = 5000,
    ) -> dict:
        """Create core.cargo rows for manifest containers that have none.

        The audit's largest data-lifecycle gap: core.igm_line_container held
        12,235 real manifested containers while core.cargo held 19 rows, so the
        import chain (IGM -> Container -> Cargo -> ... -> Release) was severed at
        step two and /api/customs/reconcile — which only ever UPDATES containers
        already present in cargo — could never bind more than those 19.

        This creates the missing rows and nothing else:
          * INSERT ... ON CONFLICT DO NOTHING keyed on the container number, so
            running it twice is a no-op and an existing cargo row is never
            overwritten (a box already moving through the yard keeps its state);
          * every new row starts at lifecycle_status 'CREATED' /
            customs_status 'PENDING' — the state machine still has to be walked,
            nothing is fast-forwarded;
          * `direction` = 'IMPORT' and `source_igm_no` record provenance (0115).

        Only ISO-6346-shaped container numbers are admitted, so manifest noise
        (blank cells, 'POWERPACK1' pseudo-containers) cannot enter the lifecycle.
        Returns {created, skipped_existing, candidates}.
        """
        where = "WHERE c.container_no ~ '^[A-Z]{4}[0-9]{7}$'"
        params: dict[str, Any] = {"lim": int(limit)}
        if igm_no:
            where += " AND c.igm_no = :igm"
            params["igm"] = str(igm_no)
        sql = f"""
            WITH candidate AS (
                SELECT DISTINCT ON (c.container_no)
                       c.container_no, c.igm_no, l.eta
                  FROM core.igm_line_container c
                  LEFT JOIN core.igm l ON l.igm_no = c.igm_no
                  {where}
                 ORDER BY c.container_no, c.igm_no DESC
                 LIMIT :lim
            ), ins AS (
                INSERT INTO core.cargo
                    (container_number, customs_status, is_released,
                     lifecycle_status, direction, source_igm_no, eta)
                SELECT container_no, 'PENDING', false, 'CREATED', 'IMPORT',
                       igm_no, eta
                  FROM candidate
                ON CONFLICT (container_number) DO NOTHING
                RETURNING container_number
            )
            SELECT (SELECT count(*) FROM candidate)          AS candidates,
                   (SELECT count(*) FROM ins)                AS created,
                   (SELECT array_agg(container_number)
                      FROM (SELECT container_number FROM ins LIMIT 50) s) AS sample
        """
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first() or {}
        candidates = int(row.get("candidates") or 0)
        created = int(row.get("created") or 0)
        return {
            "candidates": candidates,
            "created": created,
            "skipped_existing": candidates - created,
            "sample": list(row.get("sample") or [])[:50],
        }

    async def create_cargo_notification(self, container_number: str, *,
                                        notification_type: str, severity: str,
                                        message: str) -> None:
        """Reuse the EXISTING core.cargo_notification store (migration 0017) so a
        customs hold surfaces on the existing /api/cargo/notifications feed — no new
        notification system. Best-effort insert."""
        import json
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO core.cargo_notification "
                     "(container_number, notification_type, severity, message, stakeholders) "
                     "VALUES (:cn, :t, :s, :m, CAST(:st AS jsonb))"),
                {"cn": container_number, "t": notification_type, "s": severity,
                 "m": message, "st": json.dumps(["CUSTOMS", "TERMINAL_OPS"])})

    # ------------------------------------------------------------------ persist
    async def persist(self, parsed: ParsedMessage, *, source_file: str,
                      source_sha256: str, file_size: Optional[int] = None,
                      data_origin: str = "MANUAL") -> dict:
        """Persist one parsed customs message atomically + idempotently.

        Returns an import-result dict: ``{message_id, module, import_status,
        record_count, imported_count, error_count, duplicate}``. A file whose bytes
        were already imported (FOR THE SAME data_origin) returns ``duplicate=True`` /
        ``SKIPPED_DUPLICATE`` and writes nothing. A structural failure returns
        ``FAILED`` with a recorded ledger row (and no domain rows).

        ``data_origin`` ('API' | 'MANUAL') tags the ledger + every domain row this
        file contributes, so the dashboards can show the LIVE (JNPA-API) or DEMO
        (manual) corpus. Dedup is per-origin (0120), so the same bytes delivered by
        both paths are kept once each."""
        existing = await self.find_message_by_sha(source_sha256, data_origin=data_origin)
        if existing is not None:
            return {"message_id": existing["id"], "module": existing["module"],
                    "import_status": "SKIPPED_DUPLICATE",
                    "record_count": existing["record_count"],
                    "imported_count": existing["imported_count"],
                    "error_count": existing["error_count"], "duplicate": True}

        msg = parsed.message
        module = msg["module"]
        envelope = {
            "message_type": msg["message_type"], "module": module,
            "control_number": msg.get("control_number"),
            "sender_id": msg.get("sender_id"), "receiver_id": msg.get("receiver_id"),
            "message_id_code": msg.get("message_id_code"), "sent_ts": msg.get("sent_ts"),
            "primary_ref": msg.get("primary_ref"), "source_file": source_file,
            "source_sha256": source_sha256, "file_size_bytes": file_size,
            "record_count": parsed.record_count, "data_origin": data_origin,
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_MSG_INSERT), envelope)
                message_id = res.mappings().first()["id"]
                imported = await _PERSISTERS[module](self, conn, message_id, parsed.payload,
                                                     data_origin)
                status = "SUCCESS"
                await conn.execute(
                    text("UPDATE core.customs_message SET import_status = :s, "
                         "imported_count = :imp, error_count = 0, updated_at = now() "
                         "WHERE id = :id"),
                    {"s": status, "imp": imported, "id": message_id})
            return {"message_id": message_id, "module": module, "import_status": status,
                    "record_count": parsed.record_count, "imported_count": imported,
                    "error_count": 0, "duplicate": False}
        except IntegrityError as exc:
            # A concurrent import committed the same sha (same origin) first — dup.
            dup = await self.find_message_by_sha(source_sha256, data_origin=data_origin)
            if dup is not None:
                return {"message_id": dup["id"], "module": module,
                        "import_status": "SKIPPED_DUPLICATE",
                        "record_count": dup["record_count"],
                        "imported_count": dup["imported_count"],
                        "error_count": dup["error_count"], "duplicate": True}
            return await self._record_failure(envelope, str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001 — record + surface as FAILED, never partial
            log.warning("customs.persist_failed", module=module,
                        source_file=source_file, error=str(exc))
            return await self._record_failure(envelope, str(exc))

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        """Insert a FAILED ledger row in its own transaction (the domain rows were
        rolled back). Best-effort: if even this fails, surface FAILED without an id."""
        row = dict(envelope)
        row["import_status"] = "FAILED"
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_MSG_INSERT_FAILED), row)
                mid = res.mappings().first()["id"]
                await conn.execute(
                    text("INSERT INTO core.customs_import_error "
                         "(message_id, record_ref, error_code, error_detail) "
                         "VALUES (:mid, NULL, 'PERSIST_FAILED', :d)"),
                    {"mid": mid, "d": detail[:4000]})
            fail_id: Optional[int] = mid
        except Exception as exc:  # noqa: BLE001
            log.error("customs.failure_record_failed", error=str(exc))
            fail_id = None
        return {"message_id": fail_id, "module": envelope["module"],
                "import_status": "FAILED", "record_count": envelope["record_count"],
                "imported_count": 0, "error_count": 1, "duplicate": False}

    # ----------------------------------------------------- per-module persisters
    async def _persist_igm(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                           data_origin: str = "MANUAL") -> int:
        # v3: children attach by the natural key (igm_no, line_no, subline_no) —
        # no surrogate parent-id resolution needed.
        imported = 0
        for v in payload.get("vessels", []):
            vparams = {k: v.get(k) for k in _IGM_VESSEL_COLS}
            vparams["message_id"] = message_id
            vparams["data_origin"] = data_origin
            await conn.execute(text(_IGM_VESSEL_UPSERT), vparams)
            igm_no, igm_date = v.get("igm_no"), v.get("igm_date")
            line_rows = []
            for ln in v.get("lines", []):
                lr = {k: ln.get(k) for k in _IGM_LINE_COLS}
                lr.update({"igm_no": igm_no, "igm_date": igm_date,
                           "subline_no": ln.get("subline_no") or 0,
                           "data_origin": data_origin})
                line_rows.append(lr)
            await self._exec_many(conn, _IGM_LINE_INSERT, line_rows)
            cont_rows = []
            for ln in v.get("lines", []):
                for c in ln.get("containers", []):
                    cr = {k: c.get(k) for k in _IGM_CONT_COLS}
                    cr.update({"igm_no": igm_no,
                               "line_no": ln.get("line_no"),
                               "subline_no": ln.get("subline_no") or 0,
                               "data_origin": data_origin})
                    cont_rows.append(cr)
            imported += await self._bulk_counted(
                conn, _IGM_CONT_INSERT, cont_rows,
                count_sql="SELECT count(*) FROM core.igm_line_container "
                          "WHERE igm_no = CAST(:igm AS bigint)",
                count_params={"igm": igm_no})
        return imported

    async def _persist_ooc(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                           data_origin: str = "MANUAL") -> int:
        # v3: core.ooc_item carries (be_no, container_no, invoice_no, item_sr_no)
        # directly — the legacy customs_ooc_container level is flattened away.
        imported = 0
        for o in payload.get("oocs", []):
            oparams = {k: o.get(k) for k in _OOC_COLS}
            oparams["message_id"] = message_id
            oparams["data_origin"] = data_origin
            await conn.execute(text(_OOC_UPSERT), oparams)
            be_no = o.get("bill_of_entry_no")
            cont_rows = [{"bill_of_entry_no": be_no,
                          "container_no": c.get("container_no"), "iso_valid": c.get("iso_valid"),
                          "data_origin": data_origin}
                         for c in o.get("containers", [])]
            imported += await self._bulk_counted(
                conn, _OOC_CONT_INSERT, cont_rows,
                count_sql="SELECT count(DISTINCT container_no) FROM core.ooc_item "
                          "WHERE be_no = CAST(:be AS bigint)",
                count_params={"be": be_no})
            item_rows = []
            for c in o.get("containers", []):
                for it in c.get("items", []):
                    ir = {k: it.get(k) for k in _OOC_ITEM_COLS}
                    ir.update({"bill_of_entry_no": be_no,
                               "container_no": c.get("container_no"),
                               "iso_valid": c.get("iso_valid"),
                               "data_origin": data_origin})
                    item_rows.append(ir)
            await self._exec_many(conn, _OOC_ITEM_INSERT, item_rows)
        return imported

    async def _persist_smtp(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                            data_origin: str = "MANUAL") -> int:
        imported = 0
        for p in payload.get("permits", []):
            pparams = {k: p.get(k) for k in _SMTP_COLS}
            pparams["message_id"] = message_id
            pparams["data_origin"] = data_origin
            await conn.execute(text(_SMTP_UPSERT), pparams)
            line_rows = []
            for ln in p.get("lines", []):
                lr = {k: ln.get(k) for k in _SMTP_LINE_COLS}
                lr.update({"smtp_no": p.get("smtp_no"), "data_origin": data_origin})
                line_rows.append(lr)
            imported += await self._bulk_counted(
                conn, _SMTP_LINE_INSERT, line_rows,
                count_sql="SELECT count(*) FROM core.smtp_container "
                          "WHERE smtp_no = CAST(:sno AS bigint)",
                count_params={"sno": p.get("smtp_no")})
        return imported

    async def _persist_rms(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                           data_origin: str = "MANUAL") -> int:
        s = payload.get("scanlist") or {}
        sparams = {k: s.get(k) for k in _RMS_SCAN_COLS}
        sparams["message_id"] = message_id
        sparams["data_origin"] = data_origin
        res = await conn.execute(text(_RMS_SCAN_UPSERT), sparams)
        scanlist_id = res.mappings().first()["report_id"]
        cont_rows = []
        for c in payload.get("containers", []):
            cr = {k: c.get(k) for k in _RMS_CONT_COLS}
            cr["scanlist_id"] = scanlist_id
            cr["data_origin"] = data_origin
            cont_rows.append(cr)
        return await self._bulk_counted(
            conn, _RMS_CONT_INSERT, cont_rows,
            count_sql="SELECT count(*) FROM core.rms_scan_container WHERE report_id = :sid",
            count_params={"sid": scanlist_id})

    async def _persist_leo(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                           data_origin: str = "MANUAL") -> int:
        # Leaf carries message_id and is brand-new for this message, so a message-scoped
        # count is the exact number of rows this file contributed (duplicates collapse).
        rows = [{"message_id": message_id, "data_origin": data_origin,
                 **{k: r.get(k) for k in _LEO_COLS}}
                for r in payload.get("rows", [])]
        return await self._bulk_counted(
            conn, _LEO_INSERT, rows,
            count_sql="SELECT count(*) FROM core.leo WHERE message_id = :mid",
            count_params={"mid": message_id})

    async def _persist_sb(self, conn: Any, message_id: int, payload: Mapping[str, Any],
                          data_origin: str = "MANUAL") -> int:
        rows = [{"message_id": message_id, "data_origin": data_origin,
                 **{k: r.get(k) for k in _SB_COLS}}
                for r in payload.get("rows", [])]
        return await self._bulk_counted(
            conn, _SB_INSERT, rows,
            count_sql="SELECT count(*) FROM core.shipping_bill WHERE message_id = :mid",
            count_params={"mid": message_id})


# --------------------------------------------------------------------------- SQL
# Column lists (the parser dict keys that map 1:1 to table columns). message_id and
# parent ids are added by the persisters; created_at is server-managed.
def _cols(sql_cols: str) -> tuple[str, ...]:
    return tuple(c.strip() for c in sql_cols.split(",") if c.strip())


_MSG_INSERT = """
INSERT INTO core.customs_message
    (message_type, module, control_number, sender_id, receiver_id, message_id_code,
     sent_ts, primary_ref, source_file, source_sha256, file_size_bytes, record_count,
     data_origin, import_status)
VALUES
    (:message_type, :module, :control_number, :sender_id, :receiver_id, :message_id_code,
     :sent_ts, :primary_ref, :source_file, :source_sha256, :file_size_bytes, :record_count,
     :data_origin, 'PENDING')
RETURNING id
"""
_MSG_INSERT_FAILED = """
INSERT INTO core.customs_message
    (message_type, module, control_number, sender_id, receiver_id, message_id_code,
     sent_ts, primary_ref, source_file, source_sha256, file_size_bytes, record_count,
     data_origin, import_status, error_detail)
VALUES
    (:message_type, :module, :control_number, :sender_id, :receiver_id, :message_id_code,
     :sent_ts, :primary_ref, :source_file, :source_sha256, :file_size_bytes, :record_count,
     :data_origin, 'FAILED', :error_detail)
RETURNING id
"""

# IGM ------------------------------------------------------------------------
_IGM_VESSEL_COLS = _cols(
    "customs_house_code, igm_no, igm_date, imo_code, vessel_code, voyage_no, "
    "shipping_line_code, shipping_agent_code, master_name, port_of_arrival, vessel_type, "
    "total_no_of_lines, brief_cargo_desc, expected_arrival, entry_inward, terminal_operator_code")
# legacy parser keys -> core.igm columns
_IGM_VESSEL_COLMAP = {
    "customs_house_code": "customs_house", "igm_no": "igm_no", "igm_date": "igm_date",
    "imo_code": "imo_no", "vessel_code": "vessel_code", "voyage_no": "voyage_no",
    "shipping_line_code": "line_code", "shipping_agent_code": "shipping_agent",
    "master_name": "master_name", "port_of_arrival": "port_of_arrival",
    "vessel_type": "vessel_type", "total_no_of_lines": "declared_lines",
    "brief_cargo_desc": "cargo_brief", "expected_arrival": "eta",
    "entry_inward": "entry_inward_ts", "terminal_operator_code": "terminal_code",
}
_IGM_VESSEL_UPSERT = f"""
INSERT INTO core.igm
    (message_id, data_origin, {", ".join(_IGM_VESSEL_COLMAP[c] for c in _IGM_VESSEL_COLS)})
VALUES
    (:message_id, :data_origin, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
                             for c in _IGM_VESSEL_COLS)})
ON CONFLICT (igm_no) DO UPDATE SET
    eta = EXCLUDED.eta, entry_inward_ts = EXCLUDED.entry_inward_ts,
    declared_lines = EXCLUDED.declared_lines, message_id = EXCLUDED.message_id
RETURNING id
"""
_IGM_LINE_COLS = _cols(
    "line_no, subline_no, bl_no, bl_date, house_bl_no, house_bl_date, port_of_loading, "
    "port_of_destination, port_of_discharge, importer_name, importer_address, importer_state, "
    "notified_party, nature_of_cargo, item_type, cargo_movement, no_of_packages, "
    "type_of_packages, gross_weight, unit_of_weight, goods_description, mlo_code, be_regularised")
_IGM_LINE_COLMAP = {
    "line_no": "line_no", "subline_no": "subline_no", "bl_no": "bl_no",
    "bl_date": "bl_date", "house_bl_no": "house_bl_no", "house_bl_date": "house_bl_date",
    "port_of_loading": "pol", "port_of_destination": "pod",
    "port_of_discharge": "port_of_discharge", "importer_name": "importer_name",
    "importer_address": "importer_addr", "importer_state": "importer_state",
    "notified_party": "notify_party", "nature_of_cargo": "nature_of_cargo",
    "item_type": "item_type", "cargo_movement": "cargo_movement",
    "no_of_packages": "packages", "type_of_packages": "package_type",
    "gross_weight": "gross_weight", "unit_of_weight": "weight_unit",
    "goods_description": "goods_desc", "mlo_code": "mlo_code",
    "be_regularised": "be_regularised",
}
_IGM_LINE_INSERT = f"""
INSERT INTO core.igm_line
    (igm_no, data_origin, {", ".join(_IGM_LINE_COLMAP[c] for c in _IGM_LINE_COLS)})
VALUES
    (CAST(:igm_no AS bigint), :data_origin, {", ".join(f':{c}' for c in _IGM_LINE_COLS)})
ON CONFLICT (igm_no, line_no, subline_no) DO NOTHING
"""
_IGM_CONT_COLS = _cols(
    "container_no, iso_valid, seal_no, container_agent_code, container_status, "
    "no_of_packages, container_weight, iso_size_type, soc_flag")
_IGM_CONT_COLMAP = {
    "container_no": "container_no", "iso_valid": "iso_valid", "seal_no": "seal_no",
    "container_agent_code": "agent_code", "container_status": "status",
    "no_of_packages": "packages", "container_weight": "weight",
    "iso_size_type": "iso_code", "soc_flag": "soc_flag",
}
_IGM_CONT_INSERT = f"""
INSERT INTO core.igm_line_container
    (igm_no, line_no, subline_no, data_origin, {", ".join(_IGM_CONT_COLMAP[c] for c in _IGM_CONT_COLS)})
VALUES
    (CAST(:igm_no AS bigint), :line_no, :subline_no, :data_origin, {", ".join(f':{c}' for c in _IGM_CONT_COLS)})
ON CONFLICT (igm_no, line_no, subline_no, container_no) DO NOTHING
"""

# OOC ------------------------------------------------------------------------
_OOC_COLS = _cols(
    "customs_house_code, igm_no, igm_date, line_no, subline_no, bill_of_entry_no, "
    "bill_of_entry_date, document_type, ie_code, importer_name, importer_address, "
    "importer_city, pin_code, cha_code, out_of_charge_no, out_of_charge_date, "
    "out_of_charge_type, nature_of_cargo, quantity_out_of_charged, unit_of_quantity, "
    "no_of_packages, country_of_origin, assessable_value, cif_value, total_customs_duty")
_OOC_COLMAP = {
    "customs_house_code": None,  # no core column; house lives on the message envelope
    "igm_no": "igm_no", "igm_date": None, "line_no": "igm_line_no",
    "subline_no": "igm_subline_no", "bill_of_entry_no": "be_no",
    "bill_of_entry_date": "be_date", "document_type": "document_type",
    "ie_code": "iec_code", "importer_name": "importer_name",
    "importer_address": "importer_addr", "importer_city": "importer_city",
    "pin_code": "pincode", "cha_code": "cha_code", "out_of_charge_no": "ooc_no",
    "out_of_charge_date": "ooc_date", "out_of_charge_type": "ooc_type",
    "nature_of_cargo": "nature_of_cargo", "quantity_out_of_charged": "quantity",
    "unit_of_quantity": "quantity_unit", "no_of_packages": "packages",
    "country_of_origin": "country_of_origin", "assessable_value": "assessable_value",
    "cif_value": "cif_value", "total_customs_duty": "duty_paid",
}
_OOC_INS_COLS = [c for c in _OOC_COLS if _OOC_COLMAP[c]]
_OOC_UPSERT = f"""
INSERT INTO core.bill_of_entry_ooc
    (message_id, data_origin, {", ".join(_OOC_COLMAP[c] for c in _OOC_INS_COLS)})
VALUES
    (:message_id, :data_origin, {", ".join(('CAST(:bill_of_entry_no AS bigint)' if c == 'bill_of_entry_no'
                              else 'CAST(:igm_no AS bigint)' if c == 'igm_no'
                              else f':{c}') for c in _OOC_INS_COLS)})
ON CONFLICT (be_no) DO UPDATE SET
    ooc_no = EXCLUDED.ooc_no, ooc_date = EXCLUDED.ooc_date,
    message_id = EXCLUDED.message_id
RETURNING id
"""
# container placeholder row: invoice_no='' / item_sr_no=0 (the flattened level)
_OOC_CONT_INSERT = """
INSERT INTO core.ooc_item (be_no, container_no, invoice_no, item_sr_no, iso_valid, data_origin)
VALUES (CAST(:bill_of_entry_no AS bigint), :container_no, '', 0, :iso_valid, :data_origin)
ON CONFLICT (be_no, container_no, invoice_no, item_sr_no) DO NOTHING
"""
_OOC_ITEM_COLS = _cols(
    "invoice_number, item_sr_no, item_description, hs_classification, cif_value, assessable_value")
_OOC_ITEM_COLMAP = {
    "invoice_number": "invoice_no", "item_sr_no": "item_sr_no",
    "item_description": "item_desc", "hs_classification": "hs_code",
    "cif_value": "cif_value", "assessable_value": "assessable_value",
}
_OOC_ITEM_INSERT = f"""
INSERT INTO core.ooc_item
    (be_no, container_no, iso_valid, data_origin, {", ".join(_OOC_ITEM_COLMAP[c] for c in _OOC_ITEM_COLS)})
VALUES
    (CAST(:bill_of_entry_no AS bigint), :container_no, :iso_valid, :data_origin,
     {", ".join(("coalesce(:invoice_number, '')" if c == 'invoice_number'
                 else 'coalesce(:item_sr_no, 0)' if c == 'item_sr_no'
                 else f':{c}') for c in _OOC_ITEM_COLS)})
ON CONFLICT (be_no, container_no, invoice_no, item_sr_no) DO NOTHING
"""

# SMTP -----------------------------------------------------------------------
_SMTP_COLS = _cols(
    "customs_house_code, smtp_no, smtp_date, igm_no, igm_date, destination_code, "
    "carrier_code, bond_no, terminal_operator_code")
_SMTP_COLMAP = {
    "customs_house_code": "customs_house", "smtp_no": "smtp_no",
    "smtp_date": "smtp_date", "igm_no": "igm_no", "igm_date": "igm_date",
    "destination_code": "destination_icd", "carrier_code": "carrier_code",
    "bond_no": "bond_no", "terminal_operator_code": "terminal_code",
}
_SMTP_UPSERT = f"""
INSERT INTO core.smtp_permit
    (message_id, data_origin, {", ".join(_SMTP_COLMAP[c] for c in _SMTP_COLS)})
VALUES
    (:message_id, :data_origin, {", ".join(('CAST(:smtp_no AS bigint)' if c == 'smtp_no'
                              else 'CAST(:igm_no AS bigint)' if c == 'igm_no'
                              else f':{c}') for c in _SMTP_COLS)})
ON CONFLICT (smtp_no) DO UPDATE SET message_id = EXCLUDED.message_id
RETURNING id
"""
_SMTP_LINE_COLS = _cols(
    "line_no, subline_no, consignee_name, cargo_desc, container_no, iso_valid, "
    "container_type, seal_no, no_of_packages, unit_of_packages, gross_qty, unit_of_qty")
_SMTP_LINE_COLMAP = {
    "line_no": "line_no", "subline_no": "subline_no",
    "consignee_name": "consignee", "cargo_desc": "cargo_desc",
    "container_no": "container_no", "iso_valid": "iso_valid",
    "container_type": "container_type", "seal_no": "seal_no",
    "no_of_packages": "packages", "unit_of_packages": "package_unit",
    "gross_qty": "gross_qty", "unit_of_qty": "qty_unit",
}
_SMTP_LINE_INSERT = f"""
INSERT INTO core.smtp_container
    (smtp_no, igm_line_no, igm_subline_no, data_origin,
     {", ".join(_SMTP_LINE_COLMAP[c] for c in _SMTP_LINE_COLS)})
VALUES
    (CAST(:smtp_no AS bigint), :line_no, :subline_no, :data_origin,
     {", ".join(f':{c}' for c in _SMTP_LINE_COLS)})
ON CONFLICT (smtp_no, container_no) DO NOTHING
"""

# RMS ------------------------------------------------------------------------
_RMS_SCAN_COLS = _cols(
    "customs_house, shipping_line, shipping_agent, igm_no, igm_date, igm_date_raw, "
    "processing_end_date, vessel_name, subject, any_selected, selected_count")
_RMS_SCAN_COLMAP = {
    "customs_house": "customs_house", "shipping_line": "shipping_line",
    "shipping_agent": "agent_pan", "igm_no": "igm_no", "igm_date": "igm_date",
    "igm_date_raw": "igm_date_raw", "processing_end_date": "processing_end",
    "vessel_name": "vessel_name", "subject": "subject",
    "any_selected": "any_selected", "selected_count": "selected_count",
}
_RMS_SCAN_UPSERT = f"""
INSERT INTO core.rms_scan_report
    (message_id, data_origin, {", ".join(_RMS_SCAN_COLMAP[c] for c in _RMS_SCAN_COLS)})
VALUES
    (:message_id, :data_origin, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
                             for c in _RMS_SCAN_COLS)})
ON CONFLICT (igm_no) WHERE igm_no IS NOT NULL
    DO UPDATE SET selected_count = EXCLUDED.selected_count
RETURNING report_id
"""
_RMS_CONT_COLS = _cols(
    "igm_no, sl_no, container_no, iso_valid, scan_machine, scan_location, cfs_name, goods_desc")
_RMS_CONT_COLMAP = {
    "igm_no": "igm_no", "sl_no": "sl_no", "container_no": "container_no",
    "iso_valid": "iso_valid", "scan_machine": "machine_type",
    "scan_location": "scan_location", "cfs_name": "cfs_name",
    "goods_desc": "goods_desc",
}
_RMS_CONT_INSERT = f"""
INSERT INTO core.rms_scan_container
    (report_id, data_origin, {", ".join(_RMS_CONT_COLMAP[c] for c in _RMS_CONT_COLS)})
VALUES
    (:scanlist_id, :data_origin, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
                              for c in _RMS_CONT_COLS)})
ON CONFLICT (report_id, sl_no) DO UPDATE SET
    container_no  = EXCLUDED.container_no,
    iso_valid     = EXCLUDED.iso_valid,
    machine_type  = EXCLUDED.machine_type,
    scan_location = EXCLUDED.scan_location,
    cfs_name      = EXCLUDED.cfs_name,
    goods_desc    = EXCLUDED.goods_desc
"""
# ^ DO UPDATE (was DO NOTHING): a re-issued/amended selection list that changes a
# container's assigned scanner machine must not be silently discarded (audit).

# LEO / Shipping Bill --------------------------------------------------------
_LEO_COLS = _cols("sb_no, sb_date, site_id, rotation_no, leo_date, action")
_LEO_INSERT = f"""
INSERT INTO core.leo (message_id, data_origin, {", ".join(_LEO_COLS)})
VALUES (:message_id, :data_origin, {", ".join(('CAST(:sb_no AS bigint)' if c == 'sb_no' else f':{c}')
                                for c in _LEO_COLS)})
ON CONFLICT (sb_no) DO NOTHING
"""
_SB_COLS = _cols("sb_no, sb_date, site_id, action")
_SB_INSERT = f"""
INSERT INTO core.shipping_bill (message_id, data_origin, {", ".join(_SB_COLS)})
VALUES (:message_id, :data_origin, {", ".join(('CAST(:sb_no AS bigint)' if c == 'sb_no' else f':{c}')
                                for c in _SB_COLS)})
ON CONFLICT (sb_no) DO NOTHING
"""

_PERSISTERS = {
    "IGM": CustomsRepository._persist_igm,
    "OOC": CustomsRepository._persist_ooc,
    "SMTP": CustomsRepository._persist_smtp,
    "RMS": CustomsRepository._persist_rms,
    "LEO": CustomsRepository._persist_leo,
    "SHIPPING_BILL": CustomsRepository._persist_sb,
}
