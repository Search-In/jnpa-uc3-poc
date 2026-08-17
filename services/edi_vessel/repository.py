"""COARRI/COPRAR persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the 0125 tables (``core.edi_import_file`` +
``core.edi_vessel_container``). Same conventions as services/rail/repository.py:
ledger + domain rows + final status in ONE transaction, identical bytes are a
per-origin no-op (SKIPPED_DUPLICATE), domain rows ON CONFLICT DO NOTHING on
the (doc_type, document_number, container_no) natural key.

``ensure_edi_vessel_schema()`` embeds the same DDL as
infra/postgres/v3/0125_edi_vessel_container.sql (IF NOT EXISTS throughout) so
the gateway can boot the tables idempotently.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence
from gateway.datewindow import window_cond  # GAP-DATE-01 shared primitive

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.edi_vessel.repository")


def _data_origin(uploaded_by: Optional[str]) -> str:
    """LIVE (JNPA-API sync) vs DEMO (manual import) provenance tag."""
    return "API" if (uploaded_by or "").strip().lower() == "jnpa-api" else "MANUAL"


# --------------------------------------------------------------------------- DDL
# Byte-for-byte the object definitions of 0125_edi_vessel_container.sql (minus
# the BEGIN/COMMIT wrapper — split on ';' like every ensure_*_schema).
_DDL = """
CREATE TABLE IF NOT EXISTS core.edi_import_file (
    id              bigserial PRIMARY KEY,
    feed            text NOT NULL,
    physical_format text NOT NULL DEFAULT 'XML',
    source_file     text,
    source_sha256   text,
    file_size_bytes bigint,
    record_count    integer NOT NULL DEFAULT 0,
    imported_count  integer NOT NULL DEFAULT 0,
    error_count     integer NOT NULL DEFAULT 0,
    duplicate_count integer NOT NULL DEFAULT 0,
    import_status   text NOT NULL DEFAULT 'PENDING',
    error_detail    text,
    uploaded_by     text,
    source          text NOT NULL DEFAULT 'API',
    data_origin     text NOT NULL DEFAULT 'MANUAL',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT edi_import_file_feed_check
        CHECK (feed = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                 'COPARN'::text])),
    CONSTRAINT edi_import_file_status_check
        CHECK (import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text,
               'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text,
               'REJECTED'::text])),
    CONSTRAINT ck_edi_import_file_data_origin
        CHECK (data_origin IN ('API', 'MANUAL')));

CREATE UNIQUE INDEX IF NOT EXISTS uq_edi_import_file_sha_origin
    ON core.edi_import_file (source_sha256, data_origin);
CREATE INDEX IF NOT EXISTS idx_edi_import_file_feed
    ON core.edi_import_file (feed, id DESC);

CREATE TABLE IF NOT EXISTS core.edi_vessel_container (
    id              bigserial PRIMARY KEY,
    import_file_id  bigint REFERENCES core.edi_import_file(id),
    doc_type        text NOT NULL,
    direction       text,
    document_number text,
    common_ref      text,
    sender_id       text,
    vcn             text,
    terminal_code   text,
    line_code       text,
    agent_code      text,
    container_no    text NOT NULL,
    iso_code        text,
    iso_valid       boolean,
    equipment_status text,
    container_status text,
    seal_no         text,
    shipper_seal_no text,
    gross_weight    numeric,
    tare_weight     numeric,
    pol             text,
    pod             text,
    final_pod       text,
    igm_line        integer,
    igm_subline     integer,
    cargo_type      text,
    imo_class       text,
    icd_indicator   boolean,
    damage_indicator boolean,
    damage_desc     text,
    shipping_ts     timestamptz,
    landing_ts      timestamptz,
    berthing_ts     timestamptz,
    rotation_no     text,
    rotation_date   date,
    voyage          text,
    release_ts      timestamptz,
    pickup_ts       timestamptz,
    depot_code      text,
    source_file     text,
    extra           jsonb NOT NULL DEFAULT '{}'::jsonb,
    data_origin     text NOT NULL DEFAULT 'MANUAL',
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT edi_vessel_container_doc_check
        CHECK (doc_type = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                     'COPARN'::text])),
    CONSTRAINT ck_edi_vessel_container_data_origin
        CHECK (data_origin IN ('API', 'MANUAL')));

CREATE UNIQUE INDEX IF NOT EXISTS uq_edi_vessel_container
    ON core.edi_vessel_container
       (doc_type, COALESCE(document_number, ''), container_no);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_no
    ON core.edi_vessel_container (container_no);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_vcn
    ON core.edi_vessel_container (vcn);
CREATE INDEX IF NOT EXISTS idx_edi_vessel_container_origin
    ON core.edi_vessel_container (data_origin);

-- 0126 repair for tables created by an earlier boot (pre-COPARN): add the
-- COPARN columns and widen the doc/feed CHECKs. All idempotent.
ALTER TABLE core.edi_vessel_container
    ADD COLUMN IF NOT EXISTS voyage text,
    ADD COLUMN IF NOT EXISTS release_ts timestamptz,
    ADD COLUMN IF NOT EXISTS pickup_ts timestamptz,
    ADD COLUMN IF NOT EXISTS depot_code text;
ALTER TABLE core.edi_vessel_container
    DROP CONSTRAINT IF EXISTS edi_vessel_container_doc_check;
ALTER TABLE core.edi_vessel_container
    ADD CONSTRAINT edi_vessel_container_doc_check
        CHECK (doc_type = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                     'COPARN'::text]));
ALTER TABLE core.edi_import_file
    DROP CONSTRAINT IF EXISTS edi_import_file_feed_check;
ALTER TABLE core.edi_import_file
    ADD CONSTRAINT edi_import_file_feed_check
        CHECK (feed = ANY (ARRAY['COARRI'::text, 'COPRAR'::text,
                                 'COPARN'::text]))
"""


async def ensure_edi_vessel_schema(dsn: Optional[str] = None) -> None:
    """Idempotent boot DDL for the 0125 tables (gateway lifespan)."""
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for statement in _DDL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    log.info("edi_vessel_schema_ready")


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.edi_import_file
    (feed, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, error_detail, uploaded_by, source, data_origin)
VALUES
    (:feed, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, :import_status, :error_detail, :uploaded_by, :source,
     :data_origin)
RETURNING id
"""

_ROW_COLS = (
    "doc_type", "direction", "document_number", "common_ref", "sender_id",
    "vcn", "terminal_code", "line_code", "agent_code", "container_no",
    "iso_code", "iso_valid", "equipment_status", "container_status",
    "seal_no", "shipper_seal_no", "gross_weight", "tare_weight",
    "pol", "pod", "final_pod", "igm_line", "igm_subline", "cargo_type",
    "imo_class", "icd_indicator", "damage_indicator", "damage_desc",
    "shipping_ts", "landing_ts", "berthing_ts", "rotation_no",
    "rotation_date", "voyage", "release_ts", "pickup_ts", "depot_code",
    "source_file",
)

_ROW_INSERT = (
    "INSERT INTO core.edi_vessel_container "
    f"(import_file_id, {', '.join(_ROW_COLS)}, extra, data_origin) VALUES "
    f"(:import_file_id, {', '.join(':' + c for c in _ROW_COLS)}, "
    "CAST(:extra AS jsonb), :data_origin) "
    "ON CONFLICT (doc_type, COALESCE(document_number, ''), container_no) "
    "DO NOTHING"
)


def _project(record: Mapping[str, Any]) -> dict[str, Any]:
    row = {c: record.get(c) for c in _ROW_COLS}
    row["extra"] = json.dumps(record.get("extra") or {}, default=str)
    return row


class EdiVesselRepository:
    """Idempotent persistence + import ledger for COARRI/COPRAR documents."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def find_file_by_sha(self, sha256: str,
                               data_origin: Optional[str] = None) -> Optional[dict]:
        where = "source_sha256 = :sha"
        params: dict[str, Any] = {"sha": sha256}
        if data_origin is not None:
            where += " AND data_origin = :data_origin"
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, feed, source_file, import_status, record_count, "
                "imported_count, error_count, duplicate_count, created_at "
                f"FROM core.edi_import_file WHERE {where}"),
                params)).mappings().first()
        return dict(row) if row else None

    async def persist(self, feed: str, records: Sequence[Mapping[str, Any]], *,
                      source_file: str, source_sha256: str,
                      file_size: Optional[int] = None,
                      uploaded_by: Optional[str] = None,
                      source: str = "API") -> dict:
        """One document, atomically + idempotently. Mirrors RailRepository."""
        if feed not in ("COARRI", "COPRAR", "COPARN"):
            raise KeyError(f"unknown EDI feed {feed!r}")
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
        if existing is not None:
            return self._dup_envelope(existing)

        envelope = {"feed": feed, "physical_format": "XML",
                    "source_file": source_file, "source_sha256": source_sha256,
                    "file_size_bytes": file_size, "record_count": len(records),
                    "import_status": "PENDING", "error_detail": None,
                    "uploaded_by": uploaded_by, "source": source,
                    "data_origin": data_origin}
        rows = [_project(r) for r in records]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT),
                                          envelope)).mappings().first()["id"]
                imported = 0
                if rows:
                    for r in rows:
                        r["import_file_id"] = fid
                        r["data_origin"] = data_origin
                    await conn.execute(text(_ROW_INSERT), rows)
                    imported = int((await conn.execute(text(
                        "SELECT count(*) FROM core.edi_vessel_container "
                        "WHERE import_file_id = :id"),
                        {"id": fid})).scalar() or 0)
                dup = len(records) - imported
                await conn.execute(text(
                    "UPDATE core.edi_import_file SET import_status = 'SUCCESS', "
                    "imported_count = :imp, duplicate_count = :dup, "
                    "error_count = 0, updated_at = now() WHERE id = :id"),
                    {"imp": imported, "dup": dup, "id": fid})
            return {"file_id": fid, "import_status": "SUCCESS",
                    "record_count": len(records), "imported_count": imported,
                    "error_count": 0, "duplicate_count": dup,
                    "duplicate": False}
        except IntegrityError as exc:
            dup_row = await self.find_file_by_sha(source_sha256, data_origin)
            if dup_row is not None:
                return self._dup_envelope(dup_row)
            return await self._record_failure(envelope,
                                              str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001 — surface as FAILED, never partial
            log.warning("edi_vessel.persist_failed",
                        extra={"source_file": source_file, "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    @staticmethod
    def _dup_envelope(existing: Mapping[str, Any]) -> dict:
        return {"file_id": existing["id"],
                "import_status": "SKIPPED_DUPLICATE",
                "record_count": existing["record_count"],
                "imported_count": existing["imported_count"],
                "error_count": existing["error_count"],
                "duplicate_count": existing["duplicate_count"],
                "duplicate": True}

    async def _record_failure(self, envelope: Mapping[str, Any],
                              detail: str) -> dict:
        row = dict(envelope)
        row["import_status"] = "FAILED"
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT),
                                          row)).mappings().first()["id"]
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("edi_vessel.failure_record_failed",
                      extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate_count": 0, "duplicate": False}

    async def record_rejected(self, *, feed: str, source_file: str,
                              source_sha256: str, file_size: Optional[int],
                              uploaded_by: Optional[str],
                              detail: str) -> Optional[int]:
        """A structurally-rejected document as a REJECTED ledger row."""
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
        if existing is not None:
            return existing["id"]
        envelope = {"feed": feed if feed in ("COARRI", "COPRAR", "COPARN") else "COARRI",
                    "physical_format": "XML", "source_file": source_file,
                    "source_sha256": source_sha256, "file_size_bytes": file_size,
                    "record_count": 0, "import_status": "REJECTED",
                    "error_detail": detail[:4000], "uploaded_by": uploaded_by,
                    "source": "API", "data_origin": data_origin}
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT),
                                          envelope)).mappings().first()["id"]
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("edi_vessel.reject_record_failed",
                        extra={"error": str(exc)})
            return None

    # -------------------------------------------------------------- reads
    async def _paged(self, base_from: str, where: list[str],
                     params: dict[str, Any], order: str,
                     limit: int, offset: int) -> tuple[list[dict], int]:
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        async with get_engine(self._dsn).connect() as conn:
            total = int((await conn.execute(text(
                f"SELECT count(*) FROM {base_from}{clause}"),
                params)).scalar() or 0)
            rows = (await conn.execute(text(
                f"SELECT * FROM {base_from}{clause} ORDER BY {order} "
                "LIMIT :limit OFFSET :offset"),
                {**params, "limit": limit, "offset": offset})).mappings().all()
        return [dict(r) for r in rows], total

    async def list_moves(self, *, data_origin: Optional[str] = None,
                         doc_type: Optional[str] = None,
                         direction: Optional[str] = None,
                         vcn: Optional[str] = None,
                         q: Optional[str] = None,
                         limit: int = 100, offset: int = 0,
                         window: Any = None,
                         date_col: Optional[str] = None,
                         ) -> tuple[list[dict], int]:
        where, params = [], {}
        if data_origin:
            where.append("data_origin = :data_origin")
            params["data_origin"] = data_origin
        if doc_type:
            where.append("doc_type = :doc_type")
            params["doc_type"] = doc_type.strip().upper()
        if direction:
            where.append("upper(direction) = :direction")
            params["direction"] = direction.strip().upper()
        if vcn:
            where.append("upper(vcn) = :vcn")
            params["vcn"] = vcn.strip().upper()
        if q:
            where.append("(container_no ILIKE :q OR vcn ILIKE :q "
                         "OR line_code ILIKE :q OR document_number ILIKE :q)")
            params["q"] = f"%{q.strip()}%"
        # GAP-DATE-01. `date_col` is named by the CALLER — this method
        # serves several tables, and a guessed column filters the wrong
        # one, returning a plausible answer instead of an error.
        _wcond = window_cond(window, date_col, params) if date_col else None
        if _wcond:
            where.append(_wcond)

        return await self._paged("core.edi_vessel_container", where, params,
                                 "COALESCE(shipping_ts, landing_ts, "
                                 "created_at) DESC, id DESC", limit, offset)

    async def list_uploads(self, *, feed: Optional[str] = None,
                           limit: int = 50, offset: int = 0,
                           window: Any = None,
                           date_col: Optional[str] = None,
                           ) -> tuple[list[dict], int]:
        where, params = [], {}
        if feed:
            where.append("feed = :feed")
            params["feed"] = feed.strip().upper()
        # GAP-DATE-01. `date_col` is named by the CALLER — this method
        # serves several tables, and a guessed column filters the wrong
        # one, returning a plausible answer instead of an error.
        _wcond = window_cond(window, date_col, params) if date_col else None
        if _wcond:
            where.append(_wcond)

        return await self._paged("core.edi_import_file", where, params,
                                 "id DESC", limit, offset)

    async def summary(self, *, data_origin: Optional[str] = None) -> dict:
        where, params = "", {}
        if data_origin:
            where = " WHERE data_origin = :data_origin"
            params = {"data_origin": data_origin}
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT doc_type, direction, count(*) AS containers, "
                "count(DISTINCT vcn) AS vessel_calls, "
                "count(DISTINCT container_no) AS distinct_containers "
                f"FROM core.edi_vessel_container{where} "
                "GROUP BY doc_type, direction ORDER BY doc_type, direction"),
                params)).mappings().all()
        return {"by_doc": [dict(r) for r in rows]}

    async def container_view(self, container_no: str, *,
                             data_origin: Optional[str] = None) -> dict:
        cn = container_no.strip().upper()
        extra, params = "", {"cn": cn}
        if data_origin:
            extra = " AND data_origin = :data_origin"
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM core.edi_vessel_container "
                f"WHERE upper(container_no) = :cn{extra} "
                "ORDER BY COALESCE(shipping_ts, landing_ts, created_at) DESC, "
                "id DESC"),
                params)).mappings().all()
        return {"container_no": cn, "moves": [dict(r) for r in rows]}


__all__ = ["EdiVesselRepository", "ensure_edi_vessel_schema"]
