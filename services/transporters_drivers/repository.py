"""Transporters & Drivers UPLOAD persistence — raw-SQL repository over the shared engine.

The ONLY layer that speaks SQL for the Data-Upload sub-module. It writes the new
import-ledger tables (core.td_import_file / core.td_import_error, migration 0035)
and UPSERTS the valid records into the EXISTING masters — it creates NO business
tables of its own:
  * TRANSPORTER -> core.transporter      ON CONFLICT (source_company_id) DO UPDATE
  * DRIVER      -> core.driver      ON CONFLICT (licence_no_norm)  DO UPDATE

Every file imports atomically: the ledger row, all per-row upserts and the final
status update run in ONE transaction. Each per-row upsert runs inside a SAVEPOINT so
one bad row (e.g. a Transporter Code that collides with a different existing row)
records a row error and is skipped WITHOUT aborting the whole file. Re-uploading the
exact same bytes is a no-op (sha256 dedup -> SKIPPED_DUPLICATE). Mirrors
:class:`services.cfs_ecy.repository.CfsEcyRepository` for the ledger mechanics.

Injection-safe: identifiers are fixed literals; every value is a bound parameter.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.transporters_drivers.repository")


class TransportersDriversRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ---------------------------------------------------------------- dedup
    async def find_file_by_sha(self, sha256: str) -> Optional[dict]:
        """The prior ledger row for identical bytes (content-level dedup), or None."""
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, entity_type, source_file, import_status, record_count, "
                "imported_count, error_count, duplicate_count, created_at "
                "FROM core.td_import_file WHERE source_sha256 = :sha"),
                {"sha": sha256})).mappings().first()
        return dict(row) if row else None

    # ---------------------------------------------------------------- persist
    async def persist(self, records: Sequence[Mapping[str, Any]], *, entity_type: str,
                      source_file: str, source_sha256: str, physical_format: str,
                      file_size: Optional[int] = None, uploaded_by: Optional[str] = None,
                      source: str = "UPLOAD") -> dict:
        """Persist one uploaded file atomically + idempotently. Returns the outcome
        envelope incl. ``created`` / ``updated`` / ``row_errors`` (DB-level per-row
        failures the caller records + folds into the PARTIAL decision)."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return {"file_id": existing["id"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": existing["record_count"],
                    "imported_count": existing["imported_count"],
                    "error_count": existing["error_count"],
                    "duplicate_count": existing["duplicate_count"], "duplicate": True,
                    "created": 0, "updated": 0, "row_errors": []}

        envelope = {
            "entity_type": entity_type, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "record_count": len(records),
            "uploaded_by": uploaded_by, "source": source,
        }
        upsert_sql = _TRANSPORTER_UPSERT if entity_type == "TRANSPORTER" else _DRIVER_UPSERT
        row_errors: list[dict[str, Any]] = []
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT), envelope)).mappings().first()["id"]

                # DRIVER: resolve transporter_id by (lowercased) company name, once.
                tmap: dict[str, int] = {}
                if entity_type == "DRIVER":
                    for r in (await conn.execute(text(
                        "SELECT id, lower(company_name) AS lname FROM core.transporter"))).mappings().all():
                        tmap.setdefault(r["lname"], r["id"])

                created = updated = 0
                for rec in records:
                    params = (self._transporter_params(rec, fid) if entity_type == "TRANSPORTER"
                              else self._driver_params(rec, fid, tmap))
                    try:
                        async with conn.begin_nested():
                            res = (await conn.execute(text(upsert_sql), params)).first()
                        if res is not None and res[0]:
                            created += 1
                        else:
                            updated += 1
                    except Exception as exc:  # noqa: BLE001 — one bad row, not the file
                        row_errors.append({
                            "row_number": None,
                            "column_name": ("Company ID" if entity_type == "TRANSPORTER"
                                            else "Licence Number"),
                            "error_code": "row_upsert_failed",
                            "error_detail": f"{params.get('_key')}: "
                                            f"{str(getattr(exc, 'orig', exc))[:400]}",
                            "raw_value": params.get("_key"),
                        })

                imported = created + updated
                status = "PARTIAL" if row_errors else "SUCCESS"
                await conn.execute(text(
                    "UPDATE core.td_import_file SET import_status = :st, "
                    "imported_count = :imp, error_count = :err, updated_at = now() "
                    "WHERE id = :id"),
                    {"st": status, "imp": imported, "err": len(row_errors), "id": fid})
            return {"file_id": fid, "import_status": status, "record_count": len(records),
                    "imported_count": imported, "error_count": len(row_errors),
                    "duplicate_count": 0, "duplicate": False,
                    "created": created, "updated": updated, "row_errors": row_errors}
        except IntegrityError as exc:
            dup_row = await self.find_file_by_sha(source_sha256)
            if dup_row is not None:
                return {"file_id": dup_row["id"], "import_status": "SKIPPED_DUPLICATE",
                        "record_count": dup_row["record_count"],
                        "imported_count": dup_row["imported_count"],
                        "error_count": dup_row["error_count"],
                        "duplicate_count": dup_row["duplicate_count"], "duplicate": True,
                        "created": 0, "updated": 0, "row_errors": []}
            return await self._record_failure(envelope, str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001 — record + surface as FAILED, never partial
            log.warning("td.persist_failed", extra={"source_file": source_file, "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    @staticmethod
    def _transporter_params(rec: Mapping[str, Any], fid: int) -> dict[str, Any]:
        return {
            "source_company_id": rec["source_company_id"],
            "source_user_id": rec.get("source_user_id"),
            "name": rec["name"], "code": rec.get("code"), "gstin": rec.get("gstin"),
            "status": rec.get("status") or "ACTIVE",
            "contact_person": rec.get("contact_person"), "designation": rec.get("designation"),
            "email": rec.get("email"), "mobile": rec.get("mobile"),
            "address": rec.get("address"), "import_file_id": fid,
            "_key": f"company_id={rec['source_company_id']}",
        }

    @staticmethod
    def _driver_params(rec: Mapping[str, Any], fid: int, tmap: Mapping[str, int]) -> dict[str, Any]:
        company = (rec.get("company_name") or "").strip().lower()
        return {
            "licence_no": rec["licence_no"], "licence_no_norm": rec["licence_no_norm"],
            "name": rec["name"], "company_name": rec.get("company_name"),
            "transporter_id": (tmap.get(company) if company else None),
            "licence_type": rec.get("licence_type") or "HMV",
            "licence_valid_to": rec.get("licence_valid_to"), "dob": rec.get("dob"),
            "latest_pdp_number": rec.get("latest_pdp_number"),
            "status": rec.get("status") or "ACTIVE", "import_file_id": fid,
            "_key": f"licence={rec['licence_no_norm']}",
        }

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope)
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO core.td_import_error (import_file_id, record_ref, "
                    "error_code, error_detail) VALUES (:fid, NULL, 'PERSIST_FAILED', :d)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("td.failure_record_failed", extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate_count": 0, "duplicate": False,
                "created": 0, "updated": 0, "row_errors": []}

    async def record_rejected_upload(self, *, entity_type: str, physical_format: str,
                                     source_file: str, source_sha256: str,
                                     file_size: Optional[int], uploaded_by: Optional[str],
                                     detail: str, errors: Sequence[Mapping[str, Any]]) -> Optional[int]:
        """Record a structurally-rejected upload (missing required columns / no valid
        rows) as a FAILED ledger row so it appears in history, with its errors. Writes
        NO master rows. De-dupes on sha256."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return existing["id"]
        envelope = {
            "entity_type": entity_type, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "record_count": 0,
            "error_detail": detail[:4000], "uploaded_by": uploaded_by, "source": "UPLOAD",
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), envelope)).mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("td.reject_record_failed", extra={"error": str(exc)})
            return None

    async def add_row_errors(self, file_id: int, errors: Sequence[Mapping[str, Any]]) -> None:
        """Bulk-insert per-row validation errors for one upload. Best-effort."""
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
                "INSERT INTO core.td_import_error (import_file_id, record_ref, "
                "error_code, error_detail) VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        """Flip a successful import to PARTIAL when some source rows were skipped."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.td_import_file SET import_status = 'PARTIAL', "
                "error_count = :n, updated_at = now() WHERE id = :id"),
                {"n": error_count, "id": file_id})

    # ------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("entity_type", "import_status", "source"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, entity_type, physical_format, source_file, record_count, "
                "imported_count, error_count, duplicate_count, import_status, error_detail, "
                "uploaded_by, source, created_at, updated_at "
                f"FROM core.td_import_file{where} "
                "ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
            return [dict(r) for r in res.mappings().all()]

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.td_import_file{where}"), params)).scalar() or 0)

    async def get_file(self, file_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, entity_type, physical_format, source_file, source_sha256, "
                "file_size_bytes, record_count, imported_count, error_count, duplicate_count, "
                "import_status, error_detail, uploaded_by, source, created_at, updated_at "
                "FROM core.td_import_file WHERE id = :id"), {"id": file_id})).mappings().first()
        return dict(row) if row else None

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, record_ref, error_code, error_detail, created_at "
                "FROM core.td_import_error WHERE import_file_id = :id "
                "ORDER BY id LIMIT :limit OFFSET :offset"),
                {"id": file_id, "limit": limit, "offset": offset})
            return [dict(r) for r in res.mappings().all()]


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.td_import_file
    (entity_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, uploaded_by, source)
VALUES
    (:entity_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, 'PENDING', :uploaded_by, :source)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO core.td_import_file
    (entity_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, error_detail, uploaded_by, source)
VALUES
    (:entity_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, 'FAILED', :error_detail, :uploaded_by, :source)
RETURNING id
"""

# Upsert on the EXISTING unique key (source_company_id). The legacy `contact` jsonb is
# seeded to '{}' on insert and left untouched on update; COALESCE keeps a previously
# non-null field when the upload leaves that optional column blank.
_TRANSPORTER_UPSERT = """
INSERT INTO core.transporter
    (company_id, user_id, company_name, code, gstin, contact, status,
     contact_person, designation, email, mobile_number, address, import_file_id)
VALUES
    (:source_company_id, :source_user_id, :name, :code, :gstin, '{}'::jsonb, :status,
     :contact_person, :designation, :email, :mobile, :address, :import_file_id)
ON CONFLICT (company_id) DO UPDATE SET
    company_name   = EXCLUDED.company_name,
    code           = COALESCE(EXCLUDED.code, core.transporter.code),
    gstin          = COALESCE(EXCLUDED.gstin, core.transporter.gstin),
    status         = EXCLUDED.status,
    user_id        = COALESCE(EXCLUDED.user_id, core.transporter.user_id),
    contact_person = COALESCE(EXCLUDED.contact_person, core.transporter.contact_person),
    designation    = COALESCE(EXCLUDED.designation, core.transporter.designation),
    email          = COALESCE(EXCLUDED.email, core.transporter.email),
    mobile_number  = COALESCE(EXCLUDED.mobile_number, core.transporter.mobile_number),
    address        = COALESCE(EXCLUDED.address, core.transporter.address),
    import_file_id = EXCLUDED.import_file_id,
    updated_at     = now()
RETURNING (xmax = 0) AS inserted
"""

# Upsert on the EXISTING unique key (licence_no_norm).
# licence_no_norm is a GENERATED column in core.driver — never inserted directly.
# Arbiter: the partial unique index over managed rows (id < 100000000).
_DRIVER_UPSERT = """
INSERT INTO core.driver
    (licence_number, driver_name, company_name, transporter_id, licence_type,
     licence_valid_to, latest_pdp_number, date_of_birth, status, import_file_id)
VALUES
    (:licence_no, :name, :company_name, :transporter_id, :licence_type,
     :licence_valid_to, :latest_pdp_number, :dob, :status, :import_file_id)
ON CONFLICT (licence_no_norm) WHERE id < 100000000 DO UPDATE SET
    licence_number    = EXCLUDED.licence_number,
    driver_name       = EXCLUDED.driver_name,
    company_name      = COALESCE(EXCLUDED.company_name, core.driver.company_name),
    transporter_id    = COALESCE(EXCLUDED.transporter_id, core.driver.transporter_id),
    licence_type      = EXCLUDED.licence_type,
    licence_valid_to  = COALESCE(EXCLUDED.licence_valid_to, core.driver.licence_valid_to),
    latest_pdp_number = COALESCE(EXCLUDED.latest_pdp_number, core.driver.latest_pdp_number),
    date_of_birth     = COALESCE(EXCLUDED.date_of_birth, core.driver.date_of_birth),
    status            = EXCLUDED.status,
    import_file_id    = EXCLUDED.import_file_id,
    updated_at        = now()
RETURNING (xmax = 0) AS inserted
"""
