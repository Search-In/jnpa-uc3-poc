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
                          since_id: Optional[int] = None, limit: int = 100,
                          offset: int = 0) -> list[dict]:
        """Recent customs events (newest first), optionally filtered. since_id (an
        exclusive lower bound) supports a monotonic poll cursor like cargo events."""
        where = []
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        for col, val in (("module", module), ("container_no", container_no), ("event", event)):
            if val is not None:
                where.append(f"{col} = :{col}")
                params[col] = val
        if since_id is not None:
            where.append("id > :since_id")
            params["since_id"] = since_id
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        sql = ("SELECT id, event, module, reference, container_no, payload, created_at "
               f"FROM core.customs_event{clause} ORDER BY id DESC LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), params)
            return [dict(r) for r in res.mappings().all()]

    async def find_message_by_sha(self, sha256: str) -> Optional[dict]:
        """Return the existing ledger row for this content hash, or None."""
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(
                text("SELECT id, module, message_type, source_file, import_status, "
                     "record_count, imported_count, error_count, created_at "
                     "FROM core.customs_message WHERE source_sha256 = :sha"),
                {"sha": sha256})
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
            "SELECT v.id, v.igm_no, v.igm_date, v.vessel_code, v.voyage_no, "
            "v.line_code AS shipping_line_code, v.port_of_arrival, "
            "v.declared_lines AS total_no_of_lines, v.eta AS expected_arrival, "
            "v.entry_inward_ts AS entry_inward, "
            "(SELECT count(*) FROM core.igm_line l WHERE l.igm_no = v.igm_no) AS line_count, "
            "(SELECT count(*) FROM core.igm_line_container c "
            "   WHERE c.igm_no = v.igm_no) AS container_count "
            f"FROM core.igm v{where} "
            "ORDER BY v.id DESC LIMIT :limit OFFSET :offset", params)

    async def count_igm(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no",))
        return await self._count(f"SELECT count(*) FROM core.igm{where}", params)

    async def list_igm_containers(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("igm_no", "container_no"))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, igm_no, line_no, subline_no, container_no, iso_valid, seal_no, "
            "status AS container_status, packages AS no_of_packages, "
            "weight AS container_weight, iso_code AS iso_size_type "
            f"FROM core.igm_line_container{where} ORDER BY id LIMIT :limit OFFSET :offset", params)

    async def count_igm_containers(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no", "container_no"))
        return await self._count(f"SELECT count(*) FROM core.igm_line_container{where}", params)

    async def list_ooc(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("bill_of_entry_no", "igm_no", "out_of_charge_no"), alias="o")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT o.id, o.be_no AS bill_of_entry_no, o.be_date AS bill_of_entry_date, "
            "o.igm_no, o.igm_line_no AS line_no, "
            "o.ooc_no AS out_of_charge_no, o.ooc_date AS out_of_charge_date, "
            "o.importer_name, o.iec_code AS ie_code, o.cha_code, "
            "o.duty_paid AS total_customs_duty, "
            "(SELECT count(DISTINCT c.container_no) FROM core.ooc_item c WHERE c.be_no = o.be_no) AS container_count "
            f"FROM core.bill_of_entry_ooc o{where} "
            "ORDER BY o.id DESC LIMIT :limit OFFSET :offset", params)

    async def count_ooc(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("bill_of_entry_no", "igm_no", "out_of_charge_no"))
        return await self._count(f"SELECT count(*) FROM core.bill_of_entry_ooc{where}", params)

    async def list_smtp(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("smtp_no", "igm_no", "bond_no"), alias="s")
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT s.id, s.smtp_no, s.smtp_date, s.igm_no, "
            "s.destination_icd AS destination_code, s.carrier_code, s.bond_no, "
            "(SELECT count(*) FROM core.smtp_container l WHERE l.smtp_no = s.smtp_no) AS line_count "
            f"FROM core.smtp_permit s{where} "
            "ORDER BY s.id DESC LIMIT :limit OFFSET :offset", params)

    async def count_smtp(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("smtp_no", "igm_no", "bond_no"))
        return await self._count(f"SELECT count(*) FROM core.smtp_permit{where}", params)

    async def list_rms(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("igm_no",))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, igm_no, vessel_name, shipping_line, shipping_agent, "
            "processing_end AS processing_end_date, any_selected, selected_count, created_at "
            f"FROM core.rms_scan_report{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_rms(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("igm_no",))
        return await self._count(f"SELECT count(*) FROM core.rms_scan_report{where}", params)

    async def list_leo(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("sb_no",))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, sb_no, sb_date, site_id, rotation_no, leo_date, action, created_at "
            f"FROM core.leo{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_leo(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("sb_no",))
        return await self._count(f"SELECT count(*) FROM core.leo{where}", params)

    async def list_shipping_bills(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._where(filters, ("sb_no", "site_id"))
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, sb_no, sb_date, site_id, action, created_at "
            f"FROM core.shipping_bill{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_shipping_bills(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._where(filters, ("sb_no", "site_id"))
        return await self._count(f"SELECT count(*) FROM core.shipping_bill{where}", params)

    async def container_customs(self, container_no: str) -> dict:
        """The full customs view of one container: the derived status flags + every
        customs document that references it (IGM line, OOC, SMTP line, RMS selection).
        The single soft-join binding the customs layer to a box (by value)."""
        status = await self._one(
            "SELECT container_no, igm_no, declared_igm, rms_selected, ooc_cleared, smtp_bonded "
            "FROM mart.v_customs_container_status WHERE container_no = :cn", {"cn": container_no})
        # Vessel/voyage/message-id + IGM timestamps for this box, via the same
        # container -> cargo_line -> vessel join the ICEGATE adapter uses. One box maps
        # to one cargo line -> one vessel; ORDER BY id DESC LIMIT 1 picks the latest.
        vessel = await self._one(
            "SELECT v.igm_no, v.igm_date, v.vessel_code, v.voyage_no, "
            "v.line_code AS shipping_line_code, "
            "v.port_of_arrival, v.eta AS expected_arrival, "
            "v.entry_inward_ts AS entry_inward, v.message_id "
            "FROM core.igm_line_container c "
            "JOIN core.igm v ON v.igm_no = c.igm_no "
            "WHERE c.container_no = :cn ORDER BY v.id DESC LIMIT 1", {"cn": container_no})
        igm = await self._rows(
            "SELECT igm_no, line_no, container_no, seal_no, "
            "status AS container_status, iso_code AS iso_size_type "
            "FROM core.igm_line_container WHERE container_no = :cn ORDER BY id", {"cn": container_no})
        ooc = await self._rows(
            "SELECT DISTINCT o.be_no AS bill_of_entry_no, o.ooc_no AS out_of_charge_no, "
            "o.ooc_date AS out_of_charge_date, o.importer_name "
            "FROM core.ooc_item oc JOIN core.bill_of_entry_ooc o ON o.be_no = oc.be_no "
            "WHERE oc.container_no = :cn ORDER BY 1", {"cn": container_no})
        smtp = await self._rows(
            "SELECT s.smtp_no, s.bond_no, s.destination_icd AS destination_code, "
            "sl.consignee AS consignee_name "
            "FROM core.smtp_container sl JOIN core.smtp_permit s ON s.smtp_no = sl.smtp_no "
            "WHERE sl.container_no = :cn ORDER BY s.id", {"cn": container_no})
        rms = await self._rows(
            "SELECT igm_no, machine_type AS scan_machine, scan_location, cfs_name "
            "FROM core.rms_scan_container WHERE container_no = :cn ORDER BY id", {"cn": container_no})
        return {"container_no": container_no, "status": status, "vessel": vessel,
                "message_id": (vessel or {}).get("message_id"),
                "igm": igm, "ooc": ooc, "smtp": smtp, "rms": rms}

    async def summary(self) -> dict:
        """Dashboard counts across the customs layer (one round trip per table)."""
        async with get_engine(self._dsn).connect() as conn:
            async def n(sql: str) -> int:
                return int((await conn.execute(text(sql))).scalar() or 0)
            return {
                "messages": await n("SELECT count(*) FROM core.customs_message"),
                "igm_vessels": await n("SELECT count(*) FROM core.igm"),
                "igm_containers": await n("SELECT count(*) FROM core.igm_line_container"),
                "ooc": await n("SELECT count(*) FROM core.bill_of_entry_ooc"),
                "smtp": await n("SELECT count(*) FROM core.smtp_permit"),
                "smtp_lines": await n("SELECT count(*) FROM core.smtp_container"),
                "rms_scanlists": await n("SELECT count(*) FROM core.rms_scan_report"),
                "rms_containers": await n("SELECT count(*) FROM core.rms_scan_container"),
                "leo": await n("SELECT count(*) FROM core.leo"),
                "shipping_bills": await n("SELECT count(*) FROM core.shipping_bill"),
                "distinct_containers": await n("SELECT count(*) FROM mart.v_customs_container_status"),
                "failed_imports": await n("SELECT count(*) FROM core.customs_message WHERE import_status = 'FAILED'"),
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
                      source_sha256: str, file_size: Optional[int] = None) -> dict:
        """Persist one parsed customs message atomically + idempotently.

        Returns an import-result dict: ``{message_id, module, import_status,
        record_count, imported_count, error_count, duplicate}``. A file whose bytes
        were already imported returns ``duplicate=True`` / ``SKIPPED_DUPLICATE`` and
        writes nothing. A structural failure returns ``FAILED`` with a recorded
        ledger row (and no domain rows)."""
        existing = await self.find_message_by_sha(source_sha256)
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
            "record_count": parsed.record_count,
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(text(_MSG_INSERT), envelope)
                message_id = res.mappings().first()["id"]
                imported = await _PERSISTERS[module](self, conn, message_id, parsed.payload)
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
            # A concurrent import committed the same sha first — treat as duplicate.
            dup = await self.find_message_by_sha(source_sha256)
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
    async def _persist_igm(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        # v3: children attach by the natural key (igm_no, line_no, subline_no) —
        # no surrogate parent-id resolution needed.
        imported = 0
        for v in payload.get("vessels", []):
            vparams = {k: v.get(k) for k in _IGM_VESSEL_COLS}
            vparams["message_id"] = message_id
            await conn.execute(text(_IGM_VESSEL_UPSERT), vparams)
            igm_no, igm_date = v.get("igm_no"), v.get("igm_date")
            line_rows = []
            for ln in v.get("lines", []):
                lr = {k: ln.get(k) for k in _IGM_LINE_COLS}
                lr.update({"igm_no": igm_no, "igm_date": igm_date,
                           "subline_no": ln.get("subline_no") or 0})
                line_rows.append(lr)
            await self._exec_many(conn, _IGM_LINE_INSERT, line_rows)
            cont_rows = []
            for ln in v.get("lines", []):
                for c in ln.get("containers", []):
                    cr = {k: c.get(k) for k in _IGM_CONT_COLS}
                    cr.update({"igm_no": igm_no,
                               "line_no": ln.get("line_no"),
                               "subline_no": ln.get("subline_no") or 0})
                    cont_rows.append(cr)
            imported += await self._bulk_counted(
                conn, _IGM_CONT_INSERT, cont_rows,
                count_sql="SELECT count(*) FROM core.igm_line_container "
                          "WHERE igm_no = CAST(:igm AS bigint)",
                count_params={"igm": igm_no})
        return imported

    async def _persist_ooc(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        # v3: core.ooc_item carries (be_no, container_no, invoice_no, item_sr_no)
        # directly — the legacy customs_ooc_container level is flattened away.
        imported = 0
        for o in payload.get("oocs", []):
            oparams = {k: o.get(k) for k in _OOC_COLS}
            oparams["message_id"] = message_id
            await conn.execute(text(_OOC_UPSERT), oparams)
            be_no = o.get("bill_of_entry_no")
            cont_rows = [{"bill_of_entry_no": be_no,
                          "container_no": c.get("container_no"), "iso_valid": c.get("iso_valid")}
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
                               "iso_valid": c.get("iso_valid")})
                    item_rows.append(ir)
            await self._exec_many(conn, _OOC_ITEM_INSERT, item_rows)
        return imported

    async def _persist_smtp(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        imported = 0
        for p in payload.get("permits", []):
            pparams = {k: p.get(k) for k in _SMTP_COLS}
            pparams["message_id"] = message_id
            await conn.execute(text(_SMTP_UPSERT), pparams)
            line_rows = []
            for ln in p.get("lines", []):
                lr = {k: ln.get(k) for k in _SMTP_LINE_COLS}
                lr.update({"smtp_no": p.get("smtp_no")})
                line_rows.append(lr)
            imported += await self._bulk_counted(
                conn, _SMTP_LINE_INSERT, line_rows,
                count_sql="SELECT count(*) FROM core.smtp_container "
                          "WHERE smtp_no = CAST(:sno AS bigint)",
                count_params={"sno": p.get("smtp_no")})
        return imported

    async def _persist_rms(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        s = payload.get("scanlist") or {}
        sparams = {k: s.get(k) for k in _RMS_SCAN_COLS}
        sparams["message_id"] = message_id
        res = await conn.execute(text(_RMS_SCAN_UPSERT), sparams)
        scanlist_id = res.mappings().first()["report_id"]
        cont_rows = []
        for c in payload.get("containers", []):
            cr = {k: c.get(k) for k in _RMS_CONT_COLS}
            cr["scanlist_id"] = scanlist_id
            cont_rows.append(cr)
        return await self._bulk_counted(
            conn, _RMS_CONT_INSERT, cont_rows,
            count_sql="SELECT count(*) FROM core.rms_scan_container WHERE report_id = :sid",
            count_params={"sid": scanlist_id})

    async def _persist_leo(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        # Leaf carries message_id and is brand-new for this message, so a message-scoped
        # count is the exact number of rows this file contributed (duplicates collapse).
        rows = [{"message_id": message_id, **{k: r.get(k) for k in _LEO_COLS}}
                for r in payload.get("rows", [])]
        return await self._bulk_counted(
            conn, _LEO_INSERT, rows,
            count_sql="SELECT count(*) FROM core.leo WHERE message_id = :mid",
            count_params={"mid": message_id})

    async def _persist_sb(self, conn: Any, message_id: int, payload: Mapping[str, Any]) -> int:
        rows = [{"message_id": message_id, **{k: r.get(k) for k in _SB_COLS}}
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
     import_status)
VALUES
    (:message_type, :module, :control_number, :sender_id, :receiver_id, :message_id_code,
     :sent_ts, :primary_ref, :source_file, :source_sha256, :file_size_bytes, :record_count,
     'PENDING')
RETURNING id
"""
_MSG_INSERT_FAILED = """
INSERT INTO core.customs_message
    (message_type, module, control_number, sender_id, receiver_id, message_id_code,
     sent_ts, primary_ref, source_file, source_sha256, file_size_bytes, record_count,
     import_status, error_detail)
VALUES
    (:message_type, :module, :control_number, :sender_id, :receiver_id, :message_id_code,
     :sent_ts, :primary_ref, :source_file, :source_sha256, :file_size_bytes, :record_count,
     'FAILED', :error_detail)
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
    (message_id, {", ".join(_IGM_VESSEL_COLMAP[c] for c in _IGM_VESSEL_COLS)})
VALUES
    (:message_id, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
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
    (igm_no, {", ".join(_IGM_LINE_COLMAP[c] for c in _IGM_LINE_COLS)})
VALUES
    (CAST(:igm_no AS bigint), {", ".join(f':{c}' for c in _IGM_LINE_COLS)})
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
    (igm_no, line_no, subline_no, {", ".join(_IGM_CONT_COLMAP[c] for c in _IGM_CONT_COLS)})
VALUES
    (CAST(:igm_no AS bigint), :line_no, :subline_no, {", ".join(f':{c}' for c in _IGM_CONT_COLS)})
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
    (message_id, {", ".join(_OOC_COLMAP[c] for c in _OOC_INS_COLS)})
VALUES
    (:message_id, {", ".join(('CAST(:bill_of_entry_no AS bigint)' if c == 'bill_of_entry_no'
                              else 'CAST(:igm_no AS bigint)' if c == 'igm_no'
                              else f':{c}') for c in _OOC_INS_COLS)})
ON CONFLICT (be_no) DO UPDATE SET
    ooc_no = EXCLUDED.ooc_no, ooc_date = EXCLUDED.ooc_date,
    message_id = EXCLUDED.message_id
RETURNING id
"""
# container placeholder row: invoice_no='' / item_sr_no=0 (the flattened level)
_OOC_CONT_INSERT = """
INSERT INTO core.ooc_item (be_no, container_no, invoice_no, item_sr_no, iso_valid)
VALUES (CAST(:bill_of_entry_no AS bigint), :container_no, '', 0, :iso_valid)
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
    (be_no, container_no, iso_valid, {", ".join(_OOC_ITEM_COLMAP[c] for c in _OOC_ITEM_COLS)})
VALUES
    (CAST(:bill_of_entry_no AS bigint), :container_no, :iso_valid,
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
    (message_id, {", ".join(_SMTP_COLMAP[c] for c in _SMTP_COLS)})
VALUES
    (:message_id, {", ".join(('CAST(:smtp_no AS bigint)' if c == 'smtp_no'
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
    (smtp_no, igm_line_no, igm_subline_no,
     {", ".join(_SMTP_LINE_COLMAP[c] for c in _SMTP_LINE_COLS)})
VALUES
    (CAST(:smtp_no AS bigint), :line_no, :subline_no,
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
    (message_id, {", ".join(_RMS_SCAN_COLMAP[c] for c in _RMS_SCAN_COLS)})
VALUES
    (:message_id, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
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
    (report_id, {", ".join(_RMS_CONT_COLMAP[c] for c in _RMS_CONT_COLS)})
VALUES
    (:scanlist_id, {", ".join(('CAST(:igm_no AS bigint)' if c == 'igm_no' else f':{c}')
                              for c in _RMS_CONT_COLS)})
ON CONFLICT (report_id, sl_no) DO NOTHING
"""

# LEO / Shipping Bill --------------------------------------------------------
_LEO_COLS = _cols("sb_no, sb_date, site_id, rotation_no, leo_date, action")
_LEO_INSERT = f"""
INSERT INTO core.leo (message_id, {", ".join(_LEO_COLS)})
VALUES (:message_id, {", ".join(('CAST(:sb_no AS bigint)' if c == 'sb_no' else f':{c}')
                                for c in _LEO_COLS)})
ON CONFLICT (sb_no) DO NOTHING
"""
_SB_COLS = _cols("sb_no, sb_date, site_id, action")
_SB_INSERT = f"""
INSERT INTO core.shipping_bill (message_id, {", ".join(_SB_COLS)})
VALUES (:message_id, {", ".join(('CAST(:sb_no AS bigint)' if c == 'sb_no' else f':{c}')
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
