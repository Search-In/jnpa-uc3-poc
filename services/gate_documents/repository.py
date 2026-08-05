"""Gate Document persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL for the Gate Document module (EIR / PIN / Form-13,
migration 0112). Ledger mechanics mirror
:class:`services.cfs_ecy.repository.CfsEcyRepository`: one transaction per file,
sha256 content dedup at file level, row_sha256 dedup at row level
(``ON CONFLICT DO NOTHING`` -> idempotent re-import).

Injection-safe: table/column identifiers are fixed literals chosen from a
whitelist keyed by doc_type; every value is a bound parameter.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.gate_documents.repository")

# EIR and PIN have dedicated tables (approved additions). FORM-13 REUSES the
# EXISTING core.gate_capture store (capture_type='FORM13' is already in its CHECK,
# container_no is nullable, and payload jsonb holds the document fields) — no
# duplicate table is created for it.
TABLES = {"EIR": "core.eir", "PIN": "core.pin_ticket", "FORM13": "core.gate_capture"}

_COLS: dict[str, tuple[str, ...]] = {
    "EIR": ("eir_no", "eir_type", "terminal", "container_number", "iso_valid", "vessel",
            "via_no", "seal_number", "bat_lane", "truck_no", "driver_name",
            "driver_licence", "truck_in_time", "truck_out_time", "gross_weight_mt",
            "company", "cfs_from", "cfs_to", "group_code", "scanner_stamp", "remarks",
            "row_sha256", "source_file"),
    "PIN": ("pin_number", "transaction_no", "ticket_type", "terminal", "truck_no",
            "company", "container_number", "iso_valid", "line_code", "group_code",
            "yard_location", "gate", "move_type", "leg_seq", "issued_at", "remarks",
            "row_sha256", "source_file"),
}

# Form-13 fields that live inside core.gate_capture.payload (everything except the
# native columns container_no / vehicle_plate / gate_id / captured_at).
_FORM13_PAYLOAD = ("form13_no", "visit_id", "terminal", "iso_valid", "transporter_name",
                   "driver_name", "driver_licence", "in_gate", "out_gate", "direction",
                   "bat_lane", "shipping_bill_no", "gross_wt_kg", "remarks",
                   "row_sha256", "source_file")

# Truck/vehicle column per doc type (the required key).
TRUCK_COL = {"EIR": "truck_no", "PIN": "truck_no", "FORM13": "vehicle_plate"}

# Reading Form-13 back out of gate_capture with the same field names the EIR/PIN
# rows expose, so the API shape is uniform across all three document types.
_FORM13_SELECT = """
SELECT id,
       payload->>'form13_no'        AS form13_no,
       payload->>'visit_id'         AS visit_id,
       payload->>'terminal'         AS terminal,
       container_no                 AS container_number,
       (payload->>'iso_valid')::boolean AS iso_valid,
       vehicle_plate                AS vehicle_no,
       payload->>'transporter_name' AS transporter_name,
       payload->>'driver_name'      AS driver_name,
       payload->>'driver_licence'   AS driver_licence,
       payload->>'in_gate'          AS in_gate,
       payload->>'out_gate'         AS out_gate,
       payload->>'direction'        AS direction,
       payload->>'bat_lane'         AS bat_lane,
       payload->>'shipping_bill_no' AS shipping_bill_no,
       (payload->>'gross_wt_kg')::numeric AS gross_wt_kg,
       captured_at                  AS issued_at,
       payload->>'remarks'          AS remarks,
       payload->>'source_file'      AS source_file,
       (payload->>'import_file_id')::bigint AS import_file_id,
       object_path, evidence_uri, object_name,
       source_mode, created_at
FROM core.gate_capture
"""

# Form-13 lives in the shared core.gate_capture store, which holds BOTH real
# uploads (source_mode='live') and rows from the deterministic seed generator
# (source_mode='sim').
#
# Reads must NOT hide the seeded rows. Scoping to 'live' meant a container with
# a Form-13 on file reported `form13: []` — the API said "no document" when one
# demonstrably existed. Provenance is a property of the row for the caller to
# judge, not grounds for suppressing it, so every read returns `source_mode` and
# callers narrow explicitly via the optional `source` filter.
_FORM13_SCOPE = "capture_type = 'FORM13'"

# Provenance values a caller may filter on; anything else means "no filter".
FORM13_SOURCES = ("live", "sim")


def _form13_scope(source: Optional[str] = None) -> tuple[str, dict]:
    """Form-13 WHERE fragment, optionally narrowed to one provenance."""
    if source in FORM13_SOURCES:
        return f"{_FORM13_SCOPE} AND source_mode = :form13_source", {"form13_source": source}
    return _FORM13_SCOPE, {}

_FORM13_INSERT = """
INSERT INTO core.gate_capture
    (capture_type, container_no, vehicle_plate, gate_id, source_mode, status,
     captured_at, payload, object_path, evidence_uri, object_name)
VALUES
    ('FORM13', :container_number, :vehicle_no, :in_gate, 'live', 'REGISTERED',
     coalesce(:issued_at, now()), CAST(:payload AS jsonb),
     :object_path, :evidence_uri, :object_name)
ON CONFLICT (container_no, capture_type, captured_at) DO NOTHING
"""


def _insert_sql(doc_type: str) -> str:
    if doc_type == "FORM13":
        return _FORM13_INSERT
    cols = _COLS[doc_type]
    collist = ", ".join(cols) + ", import_file_id, data_origin"
    vals = ", ".join(f":{c}" for c in cols) + ", :import_file_id, :data_origin"
    # The row-hash unique index is PARTIAL (WHERE row_sha256 IS NOT NULL);
    # Postgres can only infer a partial index when the ON CONFLICT clause
    # repeats its predicate — without it every insert raises
    # InvalidColumnReferenceError.
    return (f"INSERT INTO {TABLES[doc_type]} ({collist}) VALUES ({vals}) "
            "ON CONFLICT (row_sha256) WHERE row_sha256 IS NOT NULL DO NOTHING")


def evidence_uri_for(object_path: Optional[str]) -> Optional[str]:
    """Client-facing URL for a stored evidence object (migration 0117 / fix G-2).

    The gateway proxies the private MinIO bucket at ``/api/evidence/{object_path}``
    (gateway/routers/evidence.py), so the stored reference is the bucket-relative
    key and the URI is that key behind the proxy — never the internal minio:9000
    address, which a browser cannot reach. Same convention as
    gateway/routers/violations.py:_store_evidence.
    """
    if not object_path:
        return None
    return f"/api/evidence/{str(object_path).lstrip('/')}"


def _form13_params(rec: Mapping[str, Any], import_file_id: int) -> dict[str, Any]:
    payload = {k: rec.get(k) for k in _FORM13_PAYLOAD if rec.get(k) is not None}
    payload["import_file_id"] = import_file_id
    # Evidence reference (fix G-2). Optional: a document-only capture keeps NULL.
    object_path = rec.get("object_path") or rec.get("image_file") or None
    return {"container_number": rec.get("container_number"),
            "vehicle_no": rec.get("vehicle_no"), "in_gate": rec.get("in_gate"),
            "issued_at": rec.get("issued_at"), "payload": json.dumps(payload, default=str),
            "object_path": object_path,
            "evidence_uri": evidence_uri_for(object_path),
            "object_name": rec.get("object_name") or rec.get("image_file") or None}


def _data_origin(uploaded_by: Optional[str]) -> str:
    """LIVE (JNPA-API sync) vs DEMO (manual) provenance tag for one import.

    The JNPA API sync stamps its uploads with uploaded_by='jnpa-api'; every other
    importer (dashboard upload / directory watch) is manual. Mirrors the tag the
    0120/0121 migrations backfill from uploaded_by ('API' | 'MANUAL'). This is the
    EIR/PIN counterpart to Form-13's own source_mode on core.gate_capture."""
    return "API" if (uploaded_by or "").strip().lower() == "jnpa-api" else "MANUAL"


class GateDocumentRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------- parsed source documents
    async def list_source_documents(self, *, category: Optional[str] = None,
                                    container: Optional[str] = None,
                                    limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        """The PARSED SOURCE gate documents in ``core.gate_document``.

        Distinct from the Form-13 read above, which serves ``core.gate_capture``
        — that store is 202/203 seeded (`source_mode='sim'`), whereas these 13
        rows are the customer's own Form 13 / EIR / PIN-ticket documents parsed
        verbatim from the shared corpus, with the full as-filed payload in
        ``attrs``. Read-only; nothing writes here from the API.
        """
        conds, params = [], {}
        if category:
            conds.append("doc_category = :cat")
            params["cat"] = category.strip().upper()
        if container:
            conds.append("container_no = :cn")
            params["cn"] = container.strip().upper()
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        async with get_engine(self._dsn).connect() as conn:
            total = (await conn.execute(text(
                f"SELECT count(*) FROM core.gate_document {where}"), params)).scalar()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT doc_id, doc_category, doc_variant, doc_ref, pin_no, visit_id, doc_ts, "
                "       container_no, iso_code, load_status, gross_weight_kg, seal1, seal2, "
                "       vehicle_no, bat_no, driver_name, driver_licence, transporter_name, "
                "       truck_in_ts, truck_out_ts, gate_no, yard_position, vessel_name, voyage, "
                "       pol, pod, booking_no, cfs, group_code, attrs "
                f"FROM core.gate_document {where} "
                "ORDER BY doc_category, doc_variant LIMIT :limit OFFSET :offset"),
                params)).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            # attrs is jsonb; normalise a string payload so callers always get an object.
            if isinstance(d.get("attrs"), str):
                try:
                    d["attrs"] = json.loads(d["attrs"])
                except Exception:
                    pass
            out.append(d)
        return out, int(total or 0)

    # ---------------------------------------------------------------- dedup
    async def find_file_by_sha(self, sha256: str, *,
                               data_origin: Optional[str] = None) -> Optional[dict]:
        # Dedup is PER-ORIGIN (uq_gate_doc_import_sha_origin): identical bytes from
        # both the API and a manual dump are kept once per origin, so LIVE and DEMO
        # each stay complete. None ⇒ legacy sha-only lookup (unfiltered).
        clause = " AND data_origin = :data_origin" if data_origin else ""
        params: dict[str, Any] = {"sha": sha256}
        if data_origin:
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, doc_type, source_file, import_status, record_count, "
                "imported_count, error_count, duplicate_count, created_at "
                "FROM core.gate_doc_import_file WHERE source_sha256 = :sha" + clause),
                params)).mappings().first()
        return dict(row) if row else None

    # ---------------------------------------------------------------- persist
    async def persist(self, records: Sequence[Mapping[str, Any]], *, doc_type: str,
                      source_file: str, source_sha256: str, physical_format: str,
                      file_size: Optional[int] = None, uploaded_by: Optional[str] = None,
                      duplicate_count: int = 0, source: str = "UPLOAD") -> dict:
        # Content-level dedup, but a PRIOR ATTEMPT THAT IMPORTED NOTHING must not
        # poison the file forever: without this, one FAILED/PARTIAL upload makes
        # every retry of the same bytes return SKIPPED_DUPLICATE and the file can
        # never be loaded. Such a ledger row is reused (reset to PENDING) instead.
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin=data_origin)
        retry_file_id: Optional[int] = None
        if existing is not None:
            if (existing.get("imported_count") or 0) > 0 or existing["import_status"] == "SUCCESS":
                return {"file_id": existing["id"], "import_status": "SKIPPED_DUPLICATE",
                        "record_count": existing["record_count"],
                        "imported_count": existing["imported_count"],
                        "error_count": existing["error_count"],
                        "duplicate_count": existing["duplicate_count"], "duplicate": True,
                        "row_errors": []}
            retry_file_id = existing["id"]
            log.info("gate_doc.retry_failed_upload",
                     extra={"file_id": retry_file_id, "source_file": source_file})

        envelope = {
            "doc_type": doc_type, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "record_count": len(records),
            "duplicate_count": duplicate_count,
            "uploaded_by": uploaded_by, "source": source, "data_origin": data_origin,
        }
        insert_sql = _insert_sql(doc_type)
        cols = _COLS.get(doc_type)
        key_field = "vehicle_no" if doc_type == "FORM13" else TRUCK_COL[doc_type]
        row_errors: list[dict[str, Any]] = []
        try:
            async with get_engine(self._dsn).begin() as conn:
                if retry_file_id is not None:
                    await conn.execute(text(
                        "UPDATE core.gate_doc_import_file SET import_status='PENDING', "
                        "record_count=:rc, error_count=0, error_detail=NULL, updated_at=now() "
                        "WHERE id=:id"), {"rc": len(records), "id": retry_file_id})
                    await conn.execute(text(
                        "DELETE FROM core.gate_doc_import_error WHERE import_file_id=:id"),
                        {"id": retry_file_id})
                    fid = retry_file_id
                else:
                    fid = (await conn.execute(text(_FILE_INSERT), envelope)).mappings().first()["id"]
                for rec in records:
                    if doc_type == "FORM13":
                        params = _form13_params(rec, fid)
                    else:
                        params = {c: rec.get(c) for c in cols}
                        params["import_file_id"] = fid
                        params["data_origin"] = data_origin
                    try:
                        async with conn.begin_nested():
                            await conn.execute(text(insert_sql), params)
                    except Exception as exc:  # noqa: BLE001 — one bad row, not the file
                        row_errors.append({
                            "row_number": None,
                            "column_name": key_field,
                            "error_code": "row_insert_failed",
                            "error_detail": f"{rec.get(key_field)}: "
                                            f"{str(getattr(exc, 'orig', exc))[:400]}",
                        })
                # Honest imported count: rows actually present for this file.
                if doc_type == "FORM13":
                    imported = int((await conn.execute(text(
                        f"SELECT count(*) FROM core.gate_capture WHERE {_FORM13_SCOPE} "
                        "AND (payload->>'import_file_id')::bigint = :id"),
                        {"id": fid})).scalar() or 0)
                else:
                    imported = int((await conn.execute(text(
                        f"SELECT count(*) FROM {TABLES[doc_type]} WHERE import_file_id = :id"),
                        {"id": fid})).scalar() or 0)
                status = "PARTIAL" if row_errors else "SUCCESS"
                await conn.execute(text(
                    "UPDATE core.gate_doc_import_file SET import_status = :st, "
                    "imported_count = :imp, error_count = :err, updated_at = now() "
                    "WHERE id = :id"),
                    {"st": status, "imp": imported, "err": len(row_errors), "id": fid})
            return {"file_id": fid, "import_status": status, "record_count": len(records),
                    "imported_count": imported, "error_count": len(row_errors),
                    "duplicate_count": duplicate_count, "duplicate": False,
                    "row_errors": row_errors}
        except IntegrityError as exc:
            dup = await self.find_file_by_sha(source_sha256, data_origin=data_origin)
            if dup is not None:
                return {"file_id": dup["id"], "import_status": "SKIPPED_DUPLICATE",
                        "record_count": dup["record_count"],
                        "imported_count": dup["imported_count"],
                        "error_count": dup["error_count"],
                        "duplicate_count": dup["duplicate_count"], "duplicate": True,
                        "row_errors": []}
            return await self._record_failure(envelope, str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001
            log.warning("gate_doc.persist_failed", extra={"source_file": source_file,
                                                          "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope)
        row["error_detail"] = detail[:4000]
        fail_id: Optional[int] = None
        try:
            async with get_engine(self._dsn).begin() as conn:
                fail_id = (await conn.execute(text(_FILE_INSERT_FAILED), row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO core.gate_doc_import_error (import_file_id, record_ref, "
                    "error_code, error_detail) VALUES (:fid, NULL, 'PERSIST_FAILED', :d)"),
                    {"fid": fail_id, "d": detail[:4000]})
        except Exception as exc:  # noqa: BLE001
            log.error("gate_doc.failure_record_failed", extra={"error": str(exc)})
        return {"file_id": fail_id, "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate_count": 0, "duplicate": False,
                "row_errors": []}

    async def record_rejected_upload(self, *, doc_type: str, physical_format: str,
                                     source_file: str, source_sha256: str,
                                     file_size: Optional[int], uploaded_by: Optional[str],
                                     detail: str, errors: Sequence[Mapping[str, Any]]) -> Optional[int]:
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin=data_origin)
        if existing is not None:
            return existing["id"]
        envelope = {"doc_type": doc_type, "physical_format": physical_format,
                    "source_file": source_file, "source_sha256": source_sha256,
                    "file_size_bytes": file_size, "record_count": 0,
                    "duplicate_count": 0, "error_detail": detail[:4000],
                    "uploaded_by": uploaded_by, "source": "UPLOAD", "data_origin": data_origin}
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), envelope)).mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("gate_doc.reject_record_failed", extra={"error": str(exc)})
            return None

    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        rows = [{"fid": file_id,
                 "ref": (f"row {e.get('row_number')}" if e.get("row_number") is not None
                         else e.get("column_name")),
                 "code": e.get("error_code") or "INVALID",
                 "detail": (e.get("error_detail") or "")[:2000]}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "INSERT INTO core.gate_doc_import_error (import_file_id, record_ref, "
                "error_code, error_detail) VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.gate_doc_import_file SET import_status = 'PARTIAL', "
                "error_count = :n, updated_at = now() WHERE id = :id"),
                {"n": error_count, "id": file_id})

    # ------------------------------------------------------------------ reads
    async def _rows(self, sql: str, params: Mapping[str, Any]) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params))
            return [dict(r) for r in res.mappings().all()]

    async def _count(self, sql: str, params: Mapping[str, Any]) -> int:
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(text(sql), dict(params))).scalar() or 0)

    @staticmethod
    def _doc_where(doc_type: str, f: Mapping[str, Any]) -> tuple[str, dict]:
        """Filters for EIR/PIN (native columns) and FORM13 (gate_capture + payload)."""
        form13 = doc_type == "FORM13"
        clauses: list[str] = []
        p: dict[str, Any] = {}
        if form13:
            scope, scope_params = _form13_scope(f.get("source"))
            clauses.append(scope)
            p.update(scope_params)
        if f.get("container_number"):
            clauses.append(("container_no = :cn") if form13 else "container_number = :cn")
            p["cn"] = str(f["container_number"]).strip().upper()
        if f.get("truck_no"):
            clauses.append(f"{TRUCK_COL[doc_type]} = :truck")
            p["truck"] = str(f["truck_no"]).strip().upper().replace(" ", "")
        if f.get("terminal"):
            clauses.append(("payload->>'terminal' ILIKE :terminal") if form13
                           else "terminal ILIKE :terminal")
            p["terminal"] = f"%{f['terminal']}%"
        if doc_type == "PIN" and f.get("pin_number"):
            clauses.append("pin_number = :pin")
            p["pin"] = str(f["pin_number"]).strip()
        if form13 and f.get("visit_id"):
            clauses.append("payload->>'visit_id' = :visit")
            p["visit"] = str(f["visit_id"]).strip()
        # LIVE / DEMO provenance for EIR/PIN (native data_origin column). Form-13
        # is excluded — it lives in core.gate_capture and uses source_mode instead.
        # None ⇒ no clause ⇒ SQL identical to the pre-provenance query.
        if not form13 and f.get("data_origin"):
            clauses.append("data_origin = :data_origin")
            p["data_origin"] = f["data_origin"]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), p

    # The `terminal` printed on a gate document is free text and spelled a dozen
    # ways ("Nhava Sheva IGT", "DP World Nhava Sheva ICT", "PSA Mumbai BMCT"...).
    # core.ref_terminal_alias already maps every observed spelling to a canonical
    # terminal, so resolve through it rather than string-matching a code inside
    # the label — "Nhava Sheva IGT" contains no "NSIGT", and a bare "GTI" alias
    # belongs to APMT, so substring matching gets both wrong.
    #
    # Exact alias match first; longest-contained alias as a fallback for spellings
    # not yet in the table. LATERAL + LIMIT 1 keeps it one row per document.
    _TERMINAL_CODE = (
        "LEFT JOIN LATERAL ("
        "  SELECT rt.code"
        "  FROM core.ref_terminal_alias a"
        "  JOIN core.ref_terminal rt ON rt.terminal_id = a.terminal_id"
        "  WHERE upper(btrim(d.terminal)) = upper(a.alias)"
        "     OR upper(btrim(d.terminal)) LIKE '%' || upper(a.alias) || '%'"
        "  ORDER BY (upper(btrim(d.terminal)) = upper(a.alias)) DESC,"
        "           length(a.alias) DESC"
        "  LIMIT 1"
        ") term ON true"
    )

    async def list_docs(self, doc_type: str, *, filters: Mapping[str, Any],
                        limit: int, offset: int) -> list[dict]:
        where, p = self._doc_where(doc_type, filters)
        p.update(limit=limit, offset=offset)
        if doc_type == "FORM13":
            return await self._rows(
                f"{_FORM13_SELECT}{where} ORDER BY id DESC LIMIT :limit OFFSET :offset", p)
        # EIR / PIN: carry the resolved canonical terminal alongside the raw label,
        # so a dashboard gate id (NSIGT-G1) can match on code instead of spelling.
        return await self._rows(
            f"SELECT d.*, term.code AS terminal_code "
            f"FROM {TABLES[doc_type]} d {self._TERMINAL_CODE}"
            f"{where} ORDER BY d.id DESC LIMIT :limit OFFSET :offset", p)

    async def count_docs(self, doc_type: str, *, filters: Mapping[str, Any]) -> int:
        where, p = self._doc_where(doc_type, filters)
        return await self._count(f"SELECT count(*) FROM {TABLES[doc_type]}{where}", p)

    async def docs_for_container(self, container_no: str, *,
                                 source: Optional[str] = None) -> dict:
        """Every gate document referencing one container (the box-side view).

        Returns Form-13 rows of BOTH provenances by default, each carrying
        `source_mode`; pass source='live'/'sim' to narrow. EIR and PIN report their
        real LIVE/DEMO provenance from the data_origin column (API/MANUAL)."""
        scope, scope_params = _form13_scope(source)
        cn = {"cn": container_no, **scope_params}
        return {
            "eir": await self._rows(
                "SELECT *, data_origin AS source_mode FROM core.eir WHERE container_number = :cn "
                "ORDER BY truck_in_time NULLS LAST, id", {"cn": container_no}),
            "pin": await self._rows(
                "SELECT *, data_origin AS source_mode FROM core.pin_ticket "
                "WHERE container_number = :cn ORDER BY issued_at NULLS LAST, id",
                {"cn": container_no}),
            "form13": await self._rows(
                f"{_FORM13_SELECT} WHERE {scope} AND container_no = :cn "
                "ORDER BY captured_at NULLS LAST, id", cn),
        }

    async def docs_for_truck(self, truck_no: str, *, source: Optional[str] = None) -> dict:
        """Every gate document for one truck — the client's hero view (one tractor,
        4 documents, 3 terminals, 7 days). Includes containerless documents.

        Same provenance rules as :meth:`docs_for_container`."""
        scope, scope_params = _form13_scope(source)
        t = {"t": truck_no, **scope_params}
        return {
            "eir": await self._rows(
                "SELECT *, data_origin AS source_mode FROM core.eir WHERE truck_no = :t "
                "ORDER BY truck_in_time NULLS LAST, id", {"t": truck_no}),
            "pin": await self._rows(
                "SELECT *, data_origin AS source_mode FROM core.pin_ticket WHERE truck_no = :t "
                "ORDER BY issued_at NULLS LAST, id", {"t": truck_no}),
            "form13": await self._rows(
                f"{_FORM13_SELECT} WHERE {scope} AND vehicle_plate = :t "
                "ORDER BY captured_at NULLS LAST, id", t),
        }

    async def tat_summary(self, *, terminal: Optional[str] = None) -> dict:
        """Document-derived turnaround time — the corpus ground truth (TruckIn ->
        TruckOut on the EIR), independent of the simulator-fed gate-event views."""
        where, p = ("", {})
        if terminal:
            where, p = (" AND terminal ILIKE :terminal", {"terminal": f"%{terminal}%"})
        row = (await self._rows(
            "SELECT count(*) AS samples, "
            "       round(avg(tat_minutes)) AS avg_tat_min, "
            "       percentile_cont(0.5) WITHIN GROUP (ORDER BY tat_minutes) AS median_tat_min, "
            "       min(tat_minutes) AS min_tat_min, max(tat_minutes) AS max_tat_min "
            "FROM core.eir WHERE tat_minutes IS NOT NULL" + where, p)) or [{}]
        out = row[0]
        out["by_terminal"] = await self._rows(
            "SELECT coalesce(terminal,'(unknown)') AS terminal, count(*) AS samples, "
            "       round(avg(tat_minutes)) AS avg_tat_min "
            "FROM core.eir WHERE tat_minutes IS NOT NULL" + where +
            " GROUP BY 1 ORDER BY samples DESC", p)
        out["source"] = "document"
        return out

    async def summary(self) -> dict:
        async with get_engine(self._dsn).connect() as conn:
            async def n(sql: str) -> int:
                return int((await conn.execute(text(sql))).scalar() or 0)
            return {
                "eir": await n("SELECT count(*) FROM core.eir"),
                "pin_tickets": await n("SELECT count(DISTINCT pin_number) FROM core.pin_ticket"),
                "pin_legs": await n("SELECT count(*) FROM core.pin_ticket"),
                "dual_move_tickets": await n(
                    "SELECT count(*) FROM (SELECT pin_number FROM core.pin_ticket "
                    "GROUP BY pin_number HAVING count(*) > 1) x"),
                # form13 is the TOTAL across both provenances, broken out below so
                # the count is never silently inflated by seed rows.
                "form13": await n(f"SELECT count(*) FROM core.gate_capture WHERE {_FORM13_SCOPE}"),
                "form13_live": await n(
                    f"SELECT count(*) FROM core.gate_capture WHERE {_FORM13_SCOPE} "
                    "AND source_mode = 'live'"),
                "form13_sim": await n(
                    f"SELECT count(*) FROM core.gate_capture WHERE {_FORM13_SCOPE} "
                    "AND source_mode = 'sim'"),
                "containerless_docs": await n(
                    "SELECT (SELECT count(*) FROM core.eir WHERE container_number IS NULL) + "
                    "(SELECT count(*) FROM core.pin_ticket WHERE container_number IS NULL) + "
                    f"(SELECT count(*) FROM core.gate_capture WHERE {_FORM13_SCOPE} "
                    "AND container_no IS NULL)"),
                "eir_with_tat": await n("SELECT count(*) FROM core.eir WHERE tat_minutes IS NOT NULL"),
                "files": await n("SELECT count(*) FROM core.gate_doc_import_file"),
            }

    # ------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("doc_type", "import_status", "source"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        return await self._rows(
            "SELECT id, doc_type, physical_format, source_file, record_count, "
            "imported_count, error_count, duplicate_count, import_status, error_detail, "
            f"uploaded_by, source, created_at, updated_at FROM core.gate_doc_import_file{where} "
            "ORDER BY id DESC LIMIT :limit OFFSET :offset", params)

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        return await self._count(f"SELECT count(*) FROM core.gate_doc_import_file{where}", params)

    async def get_file(self, file_id: int) -> Optional[dict]:
        rows = await self._rows(
            "SELECT * FROM core.gate_doc_import_file WHERE id = :id", {"id": file_id})
        return rows[0] if rows else None

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        return await self._rows(
            "SELECT id, record_ref, error_code, error_detail, created_at "
            "FROM core.gate_doc_import_error WHERE import_file_id = :id "
            "ORDER BY id LIMIT :limit OFFSET :offset",
            {"id": file_id, "limit": limit, "offset": offset})


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.gate_doc_import_file
    (doc_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, duplicate_count, import_status, uploaded_by, source, data_origin)
VALUES
    (:doc_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, :duplicate_count, 'PENDING', :uploaded_by, :source, :data_origin)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO core.gate_doc_import_file
    (doc_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, duplicate_count, import_status, error_detail, uploaded_by, source,
     data_origin)
VALUES
    (:doc_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, :duplicate_count, 'FAILED', :error_detail, :uploaded_by, :source,
     :data_origin)
RETURNING id
"""
