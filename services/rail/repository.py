"""Rail persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the rail tables (migration 0119):
``core.rail_import_file`` / ``core.rail_import_error`` (the import ledger,
mirroring ``core.cfs_ecy_import_file``) and the three domain tables
``core.fois_train_intimation`` / ``core.form11_entry`` /
``core.cto_manifest_entry``.

Same conventions as every repository in this tree: SQLAlchemy 2.0 async over
asyncpg, ``get_engine(self._dsn)`` per statement block, writes inside
``engine.begin()``, INSERT ... RETURNING always inside a committing
transaction. Idempotent + duplicate-safe by construction:

  * the ledger row + every domain insert + the final status update run in ONE
    transaction;
  * re-delivering identical bytes (dump OR API) is a no-op
    (``SKIPPED_DUPLICATE``) on ``source_sha256``;
  * domain rows use ``ON CONFLICT DO NOTHING`` on a null-safe natural key, so
    a row that collides with an existing one is skipped, never overwritten.

``ensure_rail_schema()`` embeds the same DDL as
infra/postgres/v3/0119_rail_tables.sql (IF NOT EXISTS throughout) so the
gateway can boot the tables idempotently — the coexistence pattern every
other module uses (see services.jnpa_sync.repository.ensure_api_ingest_schema).
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.rail.repository")


def _data_origin(uploaded_by: Optional[str]) -> str:
    """LIVE (JNPA-API sync) vs DEMO (manual import) provenance tag for a write."""
    return "API" if (uploaded_by or "").strip().lower() == "jnpa-api" else "MANUAL"


# --------------------------------------------------------------------------- DDL
# Byte-for-byte the object definitions of infra/postgres/v3/0119_rail_tables.sql
# (minus the BEGIN/COMMIT wrapper — this splits on ';' like every other
# ensure_*_schema in the tree).
_DDL = """
CREATE TABLE IF NOT EXISTS core.rail_import_file (
    id              bigserial PRIMARY KEY,
    feed            text NOT NULL,
    physical_format text NOT NULL,
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
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT rail_import_file_feed_check
        CHECK (feed = ANY (ARRAY['FOIS'::text, 'FORM11'::text, 'CTO'::text])),
    CONSTRAINT rail_import_file_status_check
        CHECK (import_status = ANY (ARRAY['PENDING'::text, 'SUCCESS'::text,
               'PARTIAL'::text, 'FAILED'::text, 'SKIPPED_DUPLICATE'::text,
               'REJECTED'::text])),
    CONSTRAINT uq_rail_import_file_sha UNIQUE (source_sha256));

CREATE INDEX IF NOT EXISTS idx_rail_import_file_feed
    ON core.rail_import_file (feed, id DESC);
CREATE INDEX IF NOT EXISTS idx_rail_import_file_status
    ON core.rail_import_file (import_status, id DESC);

CREATE TABLE IF NOT EXISTS core.rail_import_error (
    id             bigserial PRIMARY KEY,
    import_file_id bigint NOT NULL REFERENCES core.rail_import_file(id),
    record_ref     text,
    error_code     text NOT NULL,
    error_detail   text,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS idx_rail_import_error_file
    ON core.rail_import_error (import_file_id, id);

CREATE TABLE IF NOT EXISTS core.fois_train_intimation (
    id                      bigserial PRIMARY KEY,
    import_file_id          bigint REFERENCES core.rail_import_file(id),
    rake_id                 text NOT NULL,
    rake_name               text,
    units                   integer,
    station_from            text,
    station_to              text,
    zone_from               text,
    zone_to                 text,
    last_reporting_station  text,
    last_reporting_division text,
    last_reporting_zone     text,
    loaded_empty_flag       text,
    eda                     timestamptz,
    edd                     timestamptz,
    last_status_time        timestamptz,
    source_file             text,
    extra                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at              timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_fois_train_intimation
    ON core.fois_train_intimation
       (rake_id, COALESCE(eda, 'epoch'::timestamptz));
CREATE INDEX IF NOT EXISTS idx_fois_train_intimation_eda
    ON core.fois_train_intimation (eda DESC);

CREATE TABLE IF NOT EXISTS core.form11_entry (
    id             bigserial PRIMARY KEY,
    import_file_id bigint REFERENCES core.rail_import_file(id),
    terminal       text NOT NULL,
    container_no   text NOT NULL,
    iso_code       text,
    box_size       text,
    booking_number text,
    gross_weight   numeric,
    pod            text,
    line_code      text,
    icd_location   text,
    via            text,
    status         text,
    iso_valid      boolean,
    source_file    text,
    extra          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_form11_entry
    ON core.form11_entry
       (terminal, container_no, COALESCE(booking_number, ''));
CREATE INDEX IF NOT EXISTS idx_form11_entry_container
    ON core.form11_entry (container_no);

CREATE TABLE IF NOT EXISTS core.cto_manifest_entry (
    id             bigserial PRIMARY KEY,
    import_file_id bigint REFERENCES core.rail_import_file(id),
    cto_code       text NOT NULL,
    rake_no        text,
    rake_id        text,
    seq            integer,
    wagon_no       text NOT NULL,
    container_no   text,
    is_empty       boolean NOT NULL DEFAULT false,
    box_size       text,
    load_empty     text,
    line_code      text,
    weight         numeric,
    pol            text,
    pod            text,
    from_station   text,
    terminal       text,
    booking_ref    text,
    event_ts       timestamptz,
    iso_valid      boolean,
    source_file    text,
    extra          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_cto_manifest_entry
    ON core.cto_manifest_entry
       (cto_code, wagon_no, COALESCE(container_no, ''));
CREATE INDEX IF NOT EXISTS idx_cto_manifest_entry_container
    ON core.cto_manifest_entry (container_no);
"""


async def ensure_rail_schema(dsn: Optional[str] = None) -> None:
    """Idempotent boot DDL for the 0119 rail tables (gateway lifespan)."""
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for statement in _DDL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    log.info("rail_schema_ready")


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.rail_import_file
    (feed, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, uploaded_by, source, data_origin)
VALUES
    (:feed, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, 'PENDING', :uploaded_by, :source, :data_origin)
RETURNING id
"""

_FILE_INSERT_TERMINAL = """
INSERT INTO core.rail_import_file
    (feed, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, error_detail, uploaded_by, source, data_origin)
VALUES
    (:feed, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, :import_status, :error_detail, :uploaded_by, :source, :data_origin)
RETURNING id
"""

_TABLE = {
    "FOIS": "core.fois_train_intimation",
    "FORM11": "core.form11_entry",
    "CTO": "core.cto_manifest_entry",
}

_FOIS_COLS = ("rake_id", "rake_name", "units", "station_from", "station_to",
              "zone_from", "zone_to", "last_reporting_station",
              "last_reporting_division", "last_reporting_zone",
              "loaded_empty_flag", "eda", "edd", "last_status_time",
              "source_file")
_FORM11_COLS = ("terminal", "container_no", "iso_code", "box_size",
                "booking_number", "gross_weight", "pod", "line_code",
                "icd_location", "via", "status", "iso_valid", "source_file")
_CTO_COLS = ("cto_code", "rake_no", "rake_id", "seq", "wagon_no",
             "container_no", "is_empty", "box_size", "load_empty", "line_code",
             "weight", "pol", "pod", "from_station", "terminal", "booking_ref",
             "event_ts", "iso_valid", "source_file")

_CONFLICT = {
    "FOIS": "(rake_id, COALESCE(eda, 'epoch'::timestamptz))",
    "FORM11": "(terminal, container_no, COALESCE(booking_number, ''))",
    "CTO": "(cto_code, wagon_no, COALESCE(container_no, ''))",
}
_COLS = {"FOIS": _FOIS_COLS, "FORM11": _FORM11_COLS, "CTO": _CTO_COLS}


def _domain_insert(feed: str) -> str:
    cols = ("import_file_id", *_COLS[feed], "extra", "data_origin")
    placeholders = []
    for c in cols:
        placeholders.append("CAST(:extra AS jsonb)" if c == "extra" else f":{c}")
    return (f"INSERT INTO {_TABLE[feed]} ({', '.join(cols)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT {_CONFLICT[feed]} DO NOTHING")


def _project(feed: str, record: Mapping[str, Any]) -> dict[str, Any]:
    row = {c: record.get(c) for c in _COLS[feed]}
    row["extra"] = json.dumps(record.get("extra") or {}, default=str)
    return row


class RailRepository:
    """Idempotent persistence + import ledger for the rail feeds. Stateless
    apart from the DSN (engine + pool cached), safe to share."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # -------------------------------------------------------------- dedup probe
    async def find_file_by_sha(self, sha256: str,
                               data_origin: Optional[str] = None) -> Optional[dict]:
        """The prior ledger row for identical bytes (content-level dedup).

        Dedup is PER-ORIGIN when ``data_origin`` is given: LIVE (API) and DEMO
        (manual) each keep their own copy of identical bytes — see migration 0120's
        UNIQUE(source_sha256, data_origin). Omitted ⇒ match any origin (unchanged)."""
        where = "source_sha256 = :sha"
        params: dict[str, Any] = {"sha": sha256}
        if data_origin is not None:
            where += " AND data_origin = :data_origin"
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, feed, source_file, import_status, record_count, "
                "imported_count, error_count, duplicate_count, created_at "
                f"FROM core.rail_import_file WHERE {where}"),
                params)).mappings().first()
        return dict(row) if row else None

    # -------------------------------------------------------------- persist
    async def persist(self, feed: str, records: Sequence[Mapping[str, Any]], *,
                      source_file: str, source_sha256: str,
                      physical_format: str, file_size: Optional[int] = None,
                      uploaded_by: Optional[str] = None,
                      source: str = "API") -> dict:
        """Persist one rail file atomically + idempotently. Re-uploading
        identical bytes is a no-op (SKIPPED_DUPLICATE); domain rows that collide
        with an existing one are skipped (counted as duplicates), never
        overwritten. Returns the outcome envelope."""
        if feed not in _TABLE:
            raise KeyError(f"unknown rail feed {feed!r}")
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
        if existing is not None:
            return self._dup_envelope(existing)

        envelope = {"feed": feed, "physical_format": physical_format,
                    "source_file": source_file, "source_sha256": source_sha256,
                    "file_size_bytes": file_size, "record_count": len(records),
                    "uploaded_by": uploaded_by, "source": source,
                    "data_origin": data_origin}
        insert_sql = _domain_insert(feed)
        rows = [_project(feed, r) for r in records]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT),
                                          envelope)).mappings().first()["id"]
                imported = 0
                if rows:
                    for r in rows:
                        r["import_file_id"] = fid
                        r["data_origin"] = data_origin
                    await conn.execute(text(insert_sql), rows)
                    imported = int((await conn.execute(text(
                        f"SELECT count(*) FROM {_TABLE[feed]} "
                        "WHERE import_file_id = :id"),
                        {"id": fid})).scalar() or 0)
                dup = len(records) - imported
                await conn.execute(text(
                    "UPDATE core.rail_import_file SET import_status = 'SUCCESS', "
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
            log.warning("rail.persist_failed", extra={"source_file": source_file,
                                                      "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    @staticmethod
    def _dup_envelope(existing: Mapping[str, Any]) -> dict:
        return {"file_id": existing["id"], "import_status": "SKIPPED_DUPLICATE",
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
                fid = (await conn.execute(text(_FILE_INSERT_TERMINAL),
                                          row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO core.rail_import_error (import_file_id, "
                    "record_ref, error_code, error_detail) "
                    "VALUES (:fid, NULL, 'PERSIST_FAILED', :d)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("rail.failure_record_failed", extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate_count": 0, "duplicate": False}

    # -------------------------------------------------------------- rejection
    async def record_rejected(self, *, feed: str, physical_format: str,
                              source_file: str, source_sha256: str,
                              file_size: Optional[int],
                              uploaded_by: Optional[str], detail: str,
                              reason: str,
                              errors: Sequence[Mapping[str, Any]]
                              ) -> Optional[int]:
        """Record a structurally-rejected file (bad template / unreadable /
        unsupported PDF) as a REJECTED ledger row so it appears in history.
        Writes NO domain rows. De-dupes on sha256 (per-origin)."""
        data_origin = _data_origin(uploaded_by)
        existing = await self.find_file_by_sha(source_sha256, data_origin)
        if existing is not None:
            return existing["id"]
        # feed for the ledger row must satisfy the CHECK; UNSUPPORTED files map
        # to the consumer's own feed label (FORM11 handles the PDF group).
        ledger_feed = feed if feed in _TABLE else "FORM11"
        envelope = {"feed": ledger_feed, "physical_format": physical_format,
                    "source_file": source_file, "source_sha256": source_sha256,
                    "file_size_bytes": file_size, "record_count": 0,
                    "import_status": "REJECTED",
                    "error_detail": (f"{reason}: {detail}")[:4000],
                    "uploaded_by": uploaded_by, "source": "API",
                    "data_origin": data_origin}
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_TERMINAL),
                                          envelope)).mappings().first()["id"]
            await self.add_row_errors(fid, errors)
            return fid
        except Exception as exc:  # noqa: BLE001
            log.warning("rail.reject_record_failed", extra={"error": str(exc)})
            return None

    async def add_row_errors(self, file_id: int,
                             errors: Sequence[Mapping[str, Any]]) -> None:
        """Bulk-insert per-row validation errors for one file. Best-effort."""
        rows = [{"fid": file_id,
                 "ref": (f"row {e.get('row_number')}"
                         if e.get("row_number") is not None
                         else e.get("column_name")),
                 "code": e.get("error_code") or "INVALID",
                 "detail": (e.get("error_detail") or "")[:2000]}
                for e in errors]
        if not rows:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "INSERT INTO core.rail_import_error (import_file_id, "
                "record_ref, error_code, error_detail) "
                "VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        """Flip a SUCCESS import to PARTIAL when some source rows were skipped
        as invalid (the valid rows are already persisted)."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.rail_import_file SET import_status = 'PARTIAL', "
                "error_count = :n, updated_at = now() WHERE id = :id"),
                {"n": error_count, "id": file_id})

    # -------------------------------------------------------------- reads
    # Router-facing list/summary queries (gateway/routers/rail.py). data_origin
    # follows the LIVE/DEMO selector convention (gateway/data_mode.py): a value
    # narrows to that origin, None shows everything.

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

    async def list_fois(self, *, data_origin: Optional[str] = None,
                        loaded_empty: Optional[str] = None,
                        q: Optional[str] = None,
                        limit: int = 100, offset: int = 0
                        ) -> tuple[list[dict], int]:
        where, params = [], {}
        if data_origin:
            where.append("data_origin = :data_origin")
            params["data_origin"] = data_origin
        if loaded_empty:
            where.append("upper(loaded_empty_flag) = :le")
            params["le"] = loaded_empty.strip().upper()
        if q:
            where.append("(rake_id ILIKE :q OR rake_name ILIKE :q "
                         "OR station_from ILIKE :q OR station_to ILIKE :q)")
            params["q"] = f"%{q.strip()}%"
        return await self._paged("core.fois_train_intimation", where, params,
                                 "eda DESC NULLS LAST, id DESC", limit, offset)

    async def list_form11(self, *, data_origin: Optional[str] = None,
                          terminal: Optional[str] = None,
                          q: Optional[str] = None,
                          limit: int = 100, offset: int = 0
                          ) -> tuple[list[dict], int]:
        where, params = [], {}
        if data_origin:
            where.append("data_origin = :data_origin")
            params["data_origin"] = data_origin
        if terminal:
            where.append("upper(terminal) = :terminal")
            params["terminal"] = terminal.strip().upper()
        if q:
            where.append("(container_no ILIKE :q OR booking_number ILIKE :q "
                         "OR icd_location ILIKE :q)")
            params["q"] = f"%{q.strip()}%"
        return await self._paged("core.form11_entry", where, params,
                                 "id DESC", limit, offset)

    async def list_cto(self, *, data_origin: Optional[str] = None,
                       cto_code: Optional[str] = None,
                       q: Optional[str] = None,
                       limit: int = 100, offset: int = 0
                       ) -> tuple[list[dict], int]:
        where, params = [], {}
        if data_origin:
            where.append("data_origin = :data_origin")
            params["data_origin"] = data_origin
        if cto_code:
            where.append("upper(cto_code) = :cto")
            params["cto"] = cto_code.strip().upper()
        if q:
            where.append("(container_no ILIKE :q OR wagon_no ILIKE :q "
                         "OR rake_no ILIKE :q OR rake_id ILIKE :q)")
            params["q"] = f"%{q.strip()}%"
        return await self._paged("core.cto_manifest_entry", where, params,
                                 "event_ts DESC NULLS LAST, id DESC",
                                 limit, offset)

    async def list_uploads(self, *, feed: Optional[str] = None,
                           limit: int = 50, offset: int = 0
                           ) -> tuple[list[dict], int]:
        where, params = [], {}
        if feed:
            where.append("feed = :feed")
            params["feed"] = feed.strip().upper()
        return await self._paged("core.rail_import_file", where, params,
                                 "id DESC", limit, offset)

    async def summary(self, *, data_origin: Optional[str] = None) -> dict:
        """Per-feed KPIs for the rail card: row counts, inbound rakes
        (eda still ahead), latest activity timestamps."""
        where, params = "", {}
        if data_origin:
            where = " WHERE data_origin = :data_origin"
            params = {"data_origin": data_origin}
        async with get_engine(self._dsn).connect() as conn:
            fois = (await conn.execute(text(
                "SELECT count(*) AS total, "
                "count(*) FILTER (WHERE eda >= now()) AS inbound_rakes, "
                "count(DISTINCT rake_id) AS distinct_rakes, "
                "max(last_status_time) AS last_status_time, max(eda) AS max_eda "
                f"FROM core.fois_train_intimation{where}"),
                params)).mappings().first()
            form11 = (await conn.execute(text(
                "SELECT count(*) AS total, "
                "count(DISTINCT container_no) AS distinct_containers, "
                "count(DISTINCT terminal) AS terminals "
                f"FROM core.form11_entry{where}"),
                params)).mappings().first()
            cto = (await conn.execute(text(
                "SELECT count(*) AS total, "
                "count(DISTINCT COALESCE(rake_no, rake_id)) AS rakes, "
                "count(*) FILTER (WHERE is_empty) AS empties, "
                "max(event_ts) AS last_event_ts "
                f"FROM core.cto_manifest_entry{where}"),
                params)).mappings().first()
        return {"fois": dict(fois or {}), "form11": dict(form11 or {}),
                "cto": dict(cto or {})}

    async def container_rail_view(self, container_no: str, *,
                                  data_origin: Optional[str] = None) -> dict:
        """Form 11 + CTO manifest rows for one container (rail leg of the
        follow-the-box timeline)."""
        cn = container_no.strip().upper()
        extra, params = "", {"cn": cn}
        if data_origin:
            extra = " AND data_origin = :data_origin"
            params["data_origin"] = data_origin
        async with get_engine(self._dsn).connect() as conn:
            form11 = (await conn.execute(text(
                "SELECT * FROM core.form11_entry "
                f"WHERE upper(container_no) = :cn{extra} ORDER BY id DESC"),
                params)).mappings().all()
            cto = (await conn.execute(text(
                "SELECT * FROM core.cto_manifest_entry "
                f"WHERE upper(container_no) = :cn{extra} "
                "ORDER BY event_ts DESC NULLS LAST, id DESC"),
                params)).mappings().all()
        return {"container_no": cn,
                "form11": [dict(r) for r in form11],
                "cto": [dict(r) for r in cto]}


__all__ = ["RailRepository", "ensure_rail_schema"]
