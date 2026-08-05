"""Shipping-line persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the ``jnpa.sl_*`` / ``core.ref_shipping_line``
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


def _data_origin(uploaded_by: Optional[str]) -> str:
    """LIVE (JNPA-API) vs DEMO (manual) provenance tag for one write.

    The JNPA Simulated Port-Data API sync tags its imports ``uploaded_by =
    'jnpa-api'`` → ``'API'``; every other importer (dashboard upload, directory
    dump) → ``'MANUAL'``. Mirrors gateway/data_mode.resolve_data_origin on the
    read side."""
    return "API" if (uploaded_by or "").strip().lower() == "jnpa-api" else "MANUAL"


# Canonical container columns (parser dict keys that map 1:1 to table columns).

# Legacy-shaped relations over the v3 core model. Every read goes through these
# projections so response payloads stay byte-identical with the jnpa era.
_ADV_REL = """(
    SELECT a.id, a.import_file_id,
           CASE a.direction WHEN 'E' THEN 'EAL' ELSE 'IAL' END AS list_type,
           t.code AS terminal, a.container_no, a.iso_code, a.container_valid_iso,
           CASE a.load_status WHEN 'F' THEN 'FULL' WHEN 'E' THEN 'EMPTY'
                ELSE a.load_status::text END AS freight_kind,
           a.category, a.gross_weight_kg, a.weight_source_uom, a.pol, a.pod,
           a.destination, a.line_code AS shipping_line_code, a.vessel_visit,
           a.voyage, a.bl_no AS bill_of_lading, a.seal1 AS seal_no,
           a.reefer_status, a.reefer_temp, dg.imdg_class AS imdg_code,
           dg.un_number, a.group_code, a.client_code,
           a.departure_mode::text AS departure_mode, a.nominated_cfs, a.iec_code,
           a.gst_no, a.commodity_code, a.data_origin, a.created_at
    FROM core.advance_list_container a
    LEFT JOIN core.ref_terminal t ON t.terminal_id = a.terminal_id
    LEFT JOIN core.advance_list_dg dg ON dg.al_id = a.al_id AND dg.slot = 1
) adv"""

# Delivery orders over the CANONICAL v3 model (schema.sql is the source of truth).
#
# The legacy `jnpa.sl_delivery_orders` table was ONE flat row per container carrying
# the AGDORD header, the line detail and the CODECO gate event together. v3 normalises
# that into three tables, so this projection reassembles the legacy row shape from
# them and the response contract is preserved key-for-key:
#
#   core.delivery_order       parent  — AGDORD header, keyed by do_number
#   core.delivery_order_line  child   — container detail, PK (do_number, line_no)
#   core.codeco_movement      event   — terminal gate in/out (gate pass, vehicle,
#                                       equipment status, arrival/receipt)
#
# Every column below exists in schema.sql. In particular this projection does NOT
# depend on the columns added by infra/postgres/v3/0102_arch_extensions.sql, so it
# works against a database built from schema.sql alone.
#
# The CODECO join is a soft by-value join (container_no + vcn), consistent with the
# rest of this schema, and is collapsed with DISTINCT ON to the LATEST movement per
# container. Without that collapse a container with several gate movements would
# multiply its delivery-order line into several rows and inflate `total`.
_DO_REL = """(
    SELECT
        -- The canonical line table has a COMPOSITE primary key (do_number, line_no)
        -- and no surrogate integer. `id` is therefore a positional row number, kept
        -- because the response contract has always carried it and clients use it as a
        -- list key. Ordered oldest-first so the existing `ORDER BY id DESC` still
        -- yields newest-first. Nothing looks a row up by this value — no by-id
        -- delivery-order endpoint exists. `do_number` and `line_no` are exposed
        -- alongside it as the real, stable identity.
        row_number() OVER (ORDER BY hdr.do_date NULLS FIRST, hdr.do_number, ln.line_no) AS id,
        hdr.do_number,
        ln.line_no,
        -- The importer stores the CODECO common reference in the header's payload
        -- jsonb (there is no canonical column); fall back to the DO number itself.
        coalesce(hdr.payload->>'common_ref_number', hdr.do_number) AS common_ref_number,
        ln.container_no,
        ln.iso_code,
        -- No canonical column: the legacy boolean was the parser's ISO-checksum
        -- verdict, which v3 does not persist. NULL rather than a guess derived from
        -- the ISO code being non-empty, which would assert a validation never run.
        NULL::boolean AS container_valid_iso,
        cdc.equipment_status,
        -- The importer writes the agent code into the header's agency_name.
        hdr.agency_name AS shipping_agent_code,
        hdr.vcn,
        hdr.imo_no AS imo_number,
        ln.pol AS loading_port,
        ln.pod AS dest_port,
        cdc.final_pod,
        cdc.arrival_ts,
        cdc.receipt_date,
        coalesce(cdc.delivery_mode, hdr.delivery_type) AS delivery_mode,
        cdc.gate_pass_no,
        cdc.gate_pass_ts,
        cdc.vehicle_no,
        cdc.gate_no AS gate_number,
        -- v3 keeps only a DATE for the AGDORD issue; the legacy shape had a
        -- timestamp. Widened back to timestamptz so the JSON type is unchanged.
        hdr.do_date::timestamptz AS issued_ts,
        hdr.do_date::timestamptz AS created_at
    FROM core.delivery_order hdr
    JOIN core.delivery_order_line ln ON ln.do_number = hdr.do_number
    LEFT JOIN (
        SELECT DISTINCT ON (container_no, coalesce(vcn, ''))
               container_no, vcn, equipment_status, final_pod, arrival_ts,
               receipt_date, delivery_mode, gate_pass_no, gate_pass_ts, vehicle_no,
               gate_no
        FROM core.codeco_movement
        ORDER BY container_no, coalesce(vcn, ''),
                 coalesce(gate_pass_ts, arrival_ts) DESC NULLS LAST, id DESC
    ) cdc
      ON cdc.container_no = ln.container_no
     AND coalesce(cdc.vcn, '') = coalesce(hdr.vcn, '')
) sdo"""

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
        """Append one row to the append-only core.sl_event log (same pattern as
        core.customs_event). Generated ONLY from real shipping-line processing."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO core.sl_event (event, module, reference, container_no, "
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
            f"FROM core.sl_event{clause} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    # ----------------------------------------------- E-DO (delivery orders)
    #
    # The shipping line's Electronic Delivery Order (AGDORD): the authority to
    # release the box to the consignee. Exposed header-first (one row per DO)
    # rather than through the flattened container view, because the facts that
    # identify a DO — validity, agency, consignee, BL, IGM — live on the header
    # and its lines, not on the CODECO join.
    #
    # `manifest_linked` reports whether any container on the DO also appears on a
    # filed IGM. This is the ONE cross-document join that actually resolves in the
    # current corpus, so it is surfaced rather than left for the caller to derive.
    _EDO_HDR_COLS = (
        "d.do_number, d.do_date, d.valid_upto, d.vcn, d.imo_no, d.voyage_no, "
        "d.igm_no, d.igm_date, d.agency_name, d.custodian_code, d.delivery_type, "
        "d.notify_email, d.total_weight, d.weight_unit, "
        "(SELECT count(*) FROM core.delivery_order_line l "
        "   WHERE l.do_number = d.do_number) AS container_count, "
        "EXISTS (SELECT 1 FROM core.delivery_order_line l "
        "        JOIN core.igm_line_container ic ON ic.container_no = l.container_no "
        "        WHERE l.do_number = d.do_number) AS manifest_linked"
    )

    @staticmethod
    def _edo_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        if filters.get("do_number"):
            clauses.append("d.do_number = :do_number")
            params["do_number"] = str(filters["do_number"]).strip()
        if filters.get("igm_no"):
            # igm_no is bigint — bind as int so asyncpg accepts it.
            try:
                params["igm_no"] = int(str(filters["igm_no"]).strip())
                clauses.append("d.igm_no = :igm_no")
            except ValueError:
                clauses.append("false")
        if filters.get("container_no"):
            clauses.append(
                "EXISTS (SELECT 1 FROM core.delivery_order_line l "
                "        WHERE l.do_number = d.do_number AND l.container_no = :container_no)")
            params["container_no"] = str(filters["container_no"]).strip().upper()
        if filters.get("data_origin") is not None:
            clauses.append("d.data_origin = :data_origin")
            params["data_origin"] = filters["data_origin"]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_edo(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._edo_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT {self._EDO_HDR_COLS} FROM core.delivery_order d{where} "
            "ORDER BY d.do_date DESC NULLS LAST, d.do_number DESC "
            "LIMIT :limit OFFSET :offset", params)

    async def count_edo(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._edo_where(filters)
        return await self._count(f"SELECT count(*) FROM core.delivery_order d{where}", params)

    async def edo_detail(self, do_number: str) -> dict:
        """One delivery order: header + every container line, each line carrying the
        IGM line it was manifested on when that manifest is on file."""
        params = {"do": str(do_number).strip()}
        header = await self._one(
            f"SELECT {self._EDO_HDR_COLS} FROM core.delivery_order d WHERE d.do_number = :do",
            params)
        lines = await self._rows(
            "SELECT l.line_no, l.container_no, l.seal_no, l.iso_code, l.bl_no, l.bl_date, "
            "l.consignee_name, l.consignee_addr, l.cargo_desc, l.packages, l.package_code, "
            "l.gross_weight, l.pol, l.pod, l.return_empty_by, "
            "l.igm_line_no, l.igm_subline_no, "
            # Did this exact container turn up on a filed manifest? Hero C is the
            # only case in the corpus where it does.
            "ic.igm_no AS manifest_igm_no, ic.line_no AS manifest_line_no "
            "FROM core.delivery_order_line l "
            "LEFT JOIN core.igm_line_container ic ON ic.container_no = l.container_no "
            "WHERE l.do_number = :do ORDER BY l.line_no", params)
        return {"do_number": do_number, "header": header, "lines": lines}

    # ------------------------------------------------- CODECO gate movements
    #
    # The terminal's gate-out message: the container actually leaving on a truck
    # (gate pass, vehicle, gate number) plus the vessel arrival timestamp the same
    # message carries. Exposed directly rather than through the delivery-order
    # join, because a box can be gated out with NO delivery order on file — the
    # E-DO and CODECO document sets do not fully overlap in this corpus.
    #
    # ``dwell_hours`` is DERIVED here (arrival -> gate pass) so every consumer
    # reports the same number instead of each re-deriving it from timestamps.
    # The CODECO message names a gate NUMBER but not the terminal. The terminal is
    # recovered from the vessel call the same message cites:
    #     codeco.vcn -> core.vessel_call.terminal_id -> core.ref_terminal.code
    # That is what lets a dashboard gate id like "NSICT-G1" (terminal code + gate
    # number) select the movements that actually belong to it.
    _GATE_MOVE_FROM = (
        "FROM core.codeco_movement cm "
        "LEFT JOIN core.vessel_call vc ON vc.vcn = cm.vcn "
        "LEFT JOIN core.ref_terminal rt ON rt.terminal_id = vc.terminal_id"
    )
    _GATE_MOVE_COLS = (
        "cm.id, cm.container_no, cm.vcn, cm.imo_no, cm.agent_code, cm.equipment_status, "
        "cm.cargo_type, cm.iso_code, cm.pol, cm.final_pod, cm.receipt_date, cm.arrival_ts, "
        "cm.gate_pass_no, cm.gate_pass_ts, cm.vehicle_no, cm.gate_no, cm.delivery_mode, "
        "cm.seal_status, rt.code AS terminal_code, rt.pcs_code AS terminal_pcs_code, "
        "EXTRACT(EPOCH FROM (cm.gate_pass_ts - cm.arrival_ts)) / 3600.0 AS dwell_hours"
    )

    @staticmethod
    def _gate_move_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        # gate_no is text in the schema, so compare as text and trim the input.
        if filters.get("gate_no"):
            clauses.append("cm.gate_no = :gate_no")
            params["gate_no"] = str(filters["gate_no"]).strip()
        if filters.get("terminal_code"):
            clauses.append("upper(rt.code) = upper(:terminal_code)")
            params["terminal_code"] = str(filters["terminal_code"]).strip()
        if filters.get("container_no"):
            clauses.append("cm.container_no = :container_no")
            params["container_no"] = str(filters["container_no"]).strip().upper()
        if filters.get("vehicle_no"):
            clauses.append("cm.vehicle_no = :vehicle_no")
            params["vehicle_no"] = str(filters["vehicle_no"]).strip().upper()
        if filters.get("data_origin") is not None:
            clauses.append("cm.data_origin = :data_origin")
            params["data_origin"] = filters["data_origin"]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_gate_movements(self, *, filters: Mapping[str, Any],
                                  limit: int, offset: int) -> list[dict]:
        where, params = self._gate_move_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            f"SELECT {self._GATE_MOVE_COLS} {self._GATE_MOVE_FROM}{where} "
            "ORDER BY cm.gate_pass_ts DESC NULLS LAST, cm.id DESC "
            "LIMIT :limit OFFSET :offset", params)

    async def count_gate_movements(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._gate_move_where(filters)
        return await self._count(f"SELECT count(*) {self._GATE_MOVE_FROM}{where}", params)

    async def list_gate_numbers(self) -> list[dict]:
        """Gates that actually have gate-out movements, resolved to their terminal —
        drives the gate filter without hardcoding gate ids. ``gate_id`` is the
        dashboard-shaped identifier (``NSICT-G1``) so a UI gate row can match
        directly instead of guessing."""
        return await self._rows(
            "SELECT rt.code AS terminal_code, cm.gate_no, "
            "       rt.code || '-G' || cm.gate_no AS gate_id, "
            "       count(*) AS movements "
            f"{self._GATE_MOVE_FROM} "
            "WHERE cm.gate_no IS NOT NULL "
            "GROUP BY rt.code, cm.gate_no ORDER BY rt.code NULLS LAST, cm.gate_no", {})

    async def find_file_by_sha(self, sha256: str,
                               data_origin: Optional[str] = None) -> Optional[dict]:
        # Dedup is PER-ORIGIN (UNIQUE(source_sha256, data_origin)): the same bytes
        # delivered by the API and by a manual dump are kept once per origin, so
        # LIVE and DEMO are each complete. data_origin=None ⇒ identical legacy SQL.
        sql = ("SELECT id, list_type, terminal, source_file, import_status, record_count, "
               "imported_count, error_count, created_at FROM core.sl_import_file "
               "WHERE source_sha256 = :sha")
        params: dict[str, Any] = {"sha": sha256}
        if data_origin is not None:
            sql += " AND data_origin = :data_origin"
            params["data_origin"] = data_origin
        return await self._one(sql, params)

    # ------------------------------------------------------------------ persist
    async def persist(self, parsed: ParsedList, *, source_file: str, source_sha256: str,
                      physical_format: str, file_size: Optional[int] = None,
                      uploaded_by: Optional[str] = None, source: str = "DIRECTORY") -> dict:
        """Persist one parsed shipping-line file atomically + idempotently.

        ``uploaded_by``/``source`` attribute a UI upload in the ledger; the directory
        importer leaves them at their defaults (NULL / 'DIRECTORY')."""
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
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
            "data_origin": data_origin,
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_FILE_INSERT), envelope)
                file_id = res.mappings().first()["id"]
                if parsed.delivery_orders:
                    imported = await self._persist_delivery_orders(
                        conn, file_id, parsed.delivery_orders, data_origin)
                else:
                    imported = await self._persist_containers(
                        conn, file_id, parsed.containers, data_origin)
                await conn.execute(
                    text("UPDATE core.sl_import_file SET import_status = 'SUCCESS', "
                         "imported_count = :imp, error_count = 0, updated_at = now() "
                         "WHERE id = :id"), {"imp": imported, "id": file_id})
            return {"file_id": file_id, "list_type": h["list_type"], "terminal": h["terminal"],
                    "import_status": "SUCCESS", "record_count": parsed.record_count,
                    "imported_count": imported, "error_count": 0, "duplicate": False}
        except IntegrityError as exc:
            dup = await self.find_file_by_sha(source_sha256, data_origin)
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
                text("INSERT INTO core.ref_shipping_line (line_code) VALUES (:lc) "
                     "ON CONFLICT (line_code) DO UPDATE SET last_seen = now()"), rows)

    async def _persist_containers(self, conn: Any, file_id: int, containers: Sequence[dict],
                                  data_origin: str = "MANUAL") -> int:
        if not containers:
            return 0
        await self._upsert_lines(conn, {c.get("shipping_line_code") for c in containers if c.get("shipping_line_code")})
        import hashlib
        rows = []
        for c in containers:
            row = {k: c.get(k) for k in _CONTAINER_COLS}
            row["import_file_id"] = file_id
            row["data_origin"] = data_origin
            raw = json.dumps(c.get("raw") or {}, sort_keys=True, default=str)
            row["raw"] = raw
            # De-dup on the FULL source row: byte-identical rows collapse (idempotent /
            # duplicate-safe), but two rows that differ in ANY source field both persist —
            # so no source record is ever lost (e.g. one container listed under two
            # operator codes in the same list).
            row["row_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            rows.append(row)
        before = await self._scalar(
            conn, "SELECT count(*) FROM core.advance_list_container WHERE import_file_id = :id",
            {"id": file_id})
        await conn.execute(text(_CONTAINER_INSERT), rows)
        dg_rows = [{"imdg_code": r.get("imdg_code"), "un_number": r.get("un_number"),
                    "row_sha256": r["row_sha256"]}
                   for r in rows if (r.get("imdg_code") or r.get("un_number"))]
        if dg_rows:
            await conn.execute(text(_CONTAINER_DG_INSERT), dg_rows)
        after = await self._scalar(
            conn, "SELECT count(*) FROM core.advance_list_container WHERE import_file_id = :id",
            {"id": file_id})
        return after - before

    async def _persist_delivery_orders(self, conn: Any, file_id: int, orders: Sequence[dict],
                                       data_origin: str = "MANUAL") -> int:
        if not orders:
            return 0
        rows = []
        for o in orders:
            row = {k: o.get(k) for k in _DO_COLS}
            row["import_file_id"] = file_id
            row["data_origin"] = data_origin
            rows.append(row)
        # `core.delivery_order_line` has no import_file_id in the canonical schema, so
        # the inserted count is a whole-table before/after delta instead of a per-file
        # one. Equivalent here: this runs inside the single import transaction and is
        # its only writer of the table, so the delta is exactly this batch's rows.
        before = await self._scalar(conn, "SELECT count(*) FROM core.delivery_order_line", {})
        await conn.execute(text(_DO_HEADER_INSERT), rows)
        await conn.execute(text(_DO_INSERT), rows)
        after = await self._scalar(conn, "SELECT count(*) FROM core.delivery_order_line", {})
        return after - before

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope)
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_FILE_INSERT_FAILED), row)
                fid = res.mappings().first()["id"]
                await conn.execute(
                    text("INSERT INTO core.sl_import_error (import_file_id, record_ref, "
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
            "sum(imported_count) AS imported FROM core.sl_import_file "
            "GROUP BY list_type, terminal, import_status ORDER BY list_type, terminal", {})
        by_list = await self._rows(
            f"SELECT list_type, count(*) AS containers, count(DISTINCT container_no) AS distinct_containers "
            f"FROM {_ADV_REL} GROUP BY list_type ORDER BY list_type", {})
        by_terminal = await self._rows(
            f"SELECT terminal, list_type, count(*) AS containers FROM {_ADV_REL} "
            f"GROUP BY terminal, list_type ORDER BY terminal, list_type", {})
        by_category = await self._rows(
            f"SELECT category, count(*) AS n FROM {_ADV_REL} "
            f"GROUP BY category ORDER BY n DESC", {})
        top_lines = await self._rows(
            f"SELECT shipping_line_code AS line_code, count(*) AS containers "
            f"FROM {_ADV_REL} WHERE shipping_line_code IS NOT NULL "
            f"GROUP BY shipping_line_code ORDER BY containers DESC LIMIT 15", {})
        totals = await self._one(
            "SELECT (SELECT count(*) FROM core.sl_import_file) AS files, "
            "(SELECT count(*) FROM core.advance_list_container) AS advance_containers, "
            "(SELECT count(DISTINCT container_no) FROM core.advance_list_container) AS distinct_containers, "
            "(SELECT count(*) FROM core.delivery_order_line) AS delivery_orders, "
            "(SELECT count(*) FROM core.ref_shipping_line) AS shipping_lines, "
            "(SELECT count(*) FROM core.advance_list_container WHERE bl_no IS NOT NULL) AS with_bl, "
            "(SELECT count(*) FROM core.sl_import_file WHERE import_status = 'FAILED') AS failed_files", {})
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
        # LIVE/DEMO provenance filter (data_origin projected from advance_list_container).
        if filters.get("data_origin") is not None:
            clauses.append("data_origin = :data_origin")
            params["data_origin"] = filters["data_origin"]
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    _ADV_SELECT = f"SELECT * FROM {_ADV_REL}"

    async def list_containers(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._adv_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            f"{self._ADV_SELECT}{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_containers(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._adv_where(filters)
        return await self._count(f"SELECT count(*) FROM {_ADV_REL}{where}", params)

    async def container_view(self, container_no: str) -> dict:
        summary = await self._one(
            "SELECT * FROM mart.v_shipping_line_container WHERE container_no = :cn",
            {"cn": container_no})
        advance = await self._rows(
            f"{self._ADV_SELECT} WHERE container_no = :cn ORDER BY id DESC", {"cn": container_no})
        delivery = await self._rows(
            f"SELECT * FROM {_DO_REL} "
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
            f"SELECT count(*) FROM {_ADV_REL} WHERE bill_of_lading = :bl",
            {"bl": bill_of_lading})

    async def get_line(self, line_code: str) -> Optional[dict]:
        return await self._one(
            "SELECT line_code, name AS line_name, source, first_seen, last_seen, "
            "(SELECT count(*) FROM core.advance_list_container a WHERE a.line_code = s.line_code) "
            "AS container_count FROM core.ref_shipping_line s WHERE s.line_code = :lc",
            {"lc": line_code})

    async def list_lines(self, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT s.line_code, s.name AS line_name, s.source, s.first_seen, s.last_seen, "
            "(SELECT count(*) FROM core.advance_list_container a WHERE a.line_code = s.line_code) "
            "AS container_count FROM core.ref_shipping_line s "
            "ORDER BY container_count DESC, s.line_code LIMIT :limit OFFSET :offset",
            {"limit": limit, "offset": offset})

    async def count_lines(self) -> int:
        return await self._count("SELECT count(*) FROM core.ref_shipping_line", {})

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
            f"SELECT * FROM {_DO_REL}{where} "
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
        return await self._count(f"SELECT count(*) FROM {_DO_REL}{where}", params)

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
            f"FROM core.sl_import_file{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        return await self._count(f"SELECT count(*) FROM core.sl_import_file{where}", params)

    async def get_file(self, file_id: int) -> Optional[dict]:
        return await self._one(
            "SELECT id, list_type, terminal, physical_format, source_file, source_sha256, "
            "file_size_bytes, vessel_visit, voyage, line_code, direction, record_count, "
            "imported_count, error_count, import_status, error_detail, uploaded_by, source, "
            "created_at, updated_at FROM core.sl_import_file WHERE id = :id", {"id": file_id})

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT id, record_ref, error_code, error_detail, created_at "
            "FROM core.sl_import_error WHERE import_file_id = :id "
            "ORDER BY id LIMIT :limit OFFSET :offset",
            {"id": file_id, "limit": limit, "offset": offset})

    # --------------------------------------------------------------- upload helpers
    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        """Bulk-insert per-row validation errors for one upload into the EXISTING
        core.sl_import_error table (reused — no new table). Best-effort."""
        rows = [{"fid": file_id,
                 "ref": (f"row {e.get('row_number')}" if e.get("row_number") is not None else e.get("column_name")),
                 "code": e.get("error_code") or "INVALID",
                 "detail": (e.get("error_detail") or "")[:2000]}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO core.sl_import_error (import_file_id, record_ref, "
                     "error_code, error_detail) VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        """Flip a successful import to PARTIAL when some source rows were skipped as
        invalid (records the honest outcome; the valid rows are already persisted)."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("UPDATE core.sl_import_file SET import_status = 'PARTIAL', "
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
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
        if existing is not None:
            return existing["id"]
        envelope = {
            "list_type": list_type, "terminal": terminal, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "vessel_visit": None, "voyage": None,
            "line_code": None, "direction": None, "record_count": 0,
            "error_detail": detail[:4000], "uploaded_by": uploaded_by, "source": "UPLOAD",
            "data_origin": data_origin,
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
INSERT INTO core.sl_import_file
    (list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes,
     vessel_visit, voyage, line_code, direction, record_count, import_status,
     uploaded_by, source, data_origin)
VALUES
    (:list_type, :terminal, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :vessel_visit, :voyage, :line_code, :direction, :record_count, 'PENDING',
     :uploaded_by, :source, :data_origin)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO core.sl_import_file
    (list_type, terminal, physical_format, source_file, source_sha256, file_size_bytes,
     vessel_visit, voyage, line_code, direction, record_count, import_status, error_detail,
     uploaded_by, source, data_origin)
VALUES
    (:list_type, :terminal, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :vessel_visit, :voyage, :line_code, :direction, :record_count, 'FAILED', :error_detail,
     :uploaded_by, :source, :data_origin)
RETURNING id
"""

# v3: legacy upload params map onto core.advance_list_container; terminal resolves
# through ref_terminal(+alias) and DG codes split into core.advance_list_dg.
_CONTAINER_INSERT = """
INSERT INTO core.advance_list_container
    (import_file_id, direction, terminal_id, container_no, iso_code,
     container_valid_iso, load_status, category, gross_weight_kg,
     weight_source_uom, pol, pod, destination, line_code, vessel_visit, voyage,
     bl_no, seal1, reefer_status, reefer_temp, reefer_temp_unit, group_code,
     client_code, departure_mode, nominated_cfs, iec_code, gst_no,
     commodity_code, data_origin, extras, row_sha256)
VALUES
    (:import_file_id,
     CASE :list_type WHEN 'EAL' THEN 'E' ELSE 'I' END,
     coalesce((SELECT t.terminal_id FROM core.ref_terminal t
               WHERE t.code = upper(:terminal)),
              (SELECT ta.terminal_id FROM core.ref_terminal_alias ta
               WHERE ta.alias = upper(:terminal))),
     :container_no, :iso_code, :container_valid_iso,
     CASE :freight_kind WHEN 'FULL' THEN 'F' WHEN 'EMPTY' THEN 'E'
          ELSE left(:freight_kind, 1) END,
     :category, :gross_weight_kg, :weight_source_uom, :pol, :pod, :destination,
     :shipping_line_code, :vessel_visit, :voyage, :bill_of_lading, :seal_no,
     :reefer_status, :reefer_temp, left(:reefer_uom, 1), :group_code,
     :client_code, left(:departure_mode, 1), :nominated_cfs, :iec_code,
     :gst_no, :commodity_code, :data_origin, CAST(:raw AS jsonb), :row_sha256)
ON CONFLICT (import_file_id, row_sha256) DO NOTHING
"""

# DG codes ride along after the container row exists (keyed by row_sha256).
_CONTAINER_DG_INSERT = """
INSERT INTO core.advance_list_dg (al_id, slot, imdg_class, un_number)
SELECT a.al_id, 1, :imdg_code, :un_number
FROM core.advance_list_container a WHERE a.row_sha256 = :row_sha256
ON CONFLICT (al_id, slot) DO NOTHING
"""

# v3: one legacy AGDORD row = delivery-order header (dedup on do_number) + line.
_DO_HEADER_INSERT = """
INSERT INTO core.delivery_order
    (do_number, do_date, vcn, imo_no, agency_name, custodian_code, delivery_type,
     payload, import_file_id, message_type, sender_id, receiving_party, call_sign,
     stuff_destuff_flag, shipping_agent_code, vessel_country, total_containers,
     raw_xml, data_origin)
VALUES
    (coalesce(:document_number, :common_ref_number), CAST(:issued_ts AS date),
     :vcn, :imo_number, :shipping_agent_code, :ca_code, :delivery_mode,
     jsonb_build_object('common_ref_number', :common_ref_number),
     :import_file_id, :message_type, :sender_id, :receiving_party, :call_sign,
     :stuff_destuff_flag, :shipping_agent_code, :vessel_country,
     :total_containers, :raw_xml, :data_origin)
ON CONFLICT (do_number) DO NOTHING
"""

_DO_INSERT = """
INSERT INTO core.delivery_order_line
    (do_number, line_no, container_no, iso_code, cargo_desc, pol, pod,
     import_file_id, common_ref_number, vcn, imo_number, shipping_agent_code,
     final_pod, container_valid_iso, equipment_status, cargo_type, arrival_ts,
     receipt_date, delivery_mode, gate_pass_no, gate_pass_ts, vehicle_no,
     gate_number, ca_code, con_seal_status, issued_ts, data_origin)
VALUES
    (coalesce(:document_number, :common_ref_number),
     (SELECT coalesce(max(l2.line_no), 0) + 1 FROM core.delivery_order_line l2
       WHERE l2.do_number = coalesce(:document_number, :common_ref_number)),
     :container_no, :iso_code, :cargo_type, :loading_port, :dest_port,
     :import_file_id, :common_ref_number, :vcn, :imo_number,
     :shipping_agent_code, :final_pod, :container_valid_iso, :equipment_status,
     :cargo_type, :arrival_ts, :receipt_date, :delivery_mode, :gate_pass_no,
     :gate_pass_ts, :vehicle_no, :gate_number, :ca_code, :con_seal_status,
     :issued_ts, :data_origin)
ON CONFLICT (COALESCE(common_ref_number, ''), container_no, COALESCE(gate_pass_no, '')) DO NOTHING
"""
