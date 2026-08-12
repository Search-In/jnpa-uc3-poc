"""DAO for the JNPA API sync tables (migration 0124) + the cross-ledger
dedup probe and the per-group advisory lock.

Same conventions as every repository in this tree: SQLAlchemy 2.0 async over
asyncpg, `get_engine(self._dsn)` per statement block, writes inside
`engine.begin()`, INSERT ... RETURNING always executed inside a committing
transaction (see jnpa_shared/db.py:90 for why).

`ensure_api_ingest_schema()` embeds the same DDL as
infra/postgres/v3/0124_jnpa_api_ingest.sql (IF NOT EXISTS throughout) so the
gateway can boot the tables idempotently — the coexistence pattern every
other module uses (e.g. gate_documents / mig 0112).
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import date as _date, datetime
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence, Union

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.jnpa_sync.repository")


def _as_datetime(value: Union[str, datetime, None]) -> Optional[datetime]:
    """ISO string -> datetime for asyncpg's strict timestamptz binding.

    Mirrors ``service._parse_ts``: an unparseable value becomes None so the
    caller's COALESCE(..., now()) supplies the timestamp rather than the whole
    row failing.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None

_DDL = """
CREATE TABLE IF NOT EXISTS core.api_sync_state (
    group_slug      text PRIMARY KEY,
    watermark_ts    timestamptz,
    last_cursor     text,
    last_run_id     bigint,
    last_status     text,
    updated_at      timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS core.api_ingest_run (
    id                       bigserial PRIMARY KEY,
    started_at               timestamptz NOT NULL DEFAULT now(),
    finished_at              timestamptz,
    trigger                  text NOT NULL,
    group_slug               text,
    status                   text NOT NULL DEFAULT 'RUNNING',
    api_mode                 text NOT NULL DEFAULT 'LIVE',
    records_listed           integer NOT NULL DEFAULT 0,
    records_new              integer NOT NULL DEFAULT 0,
    records_duplicate        integer NOT NULL DEFAULT 0,
    files_downloaded         integer NOT NULL DEFAULT 0,
    files_304                integer NOT NULL DEFAULT 0,
    files_skipped_checksum   integer NOT NULL DEFAULT 0,
    bytes_downloaded         bigint  NOT NULL DEFAULT 0,
    request_count            integer NOT NULL DEFAULT 0,
    rate_limit_remaining_min integer,
    error                    text,
    detail                   jsonb NOT NULL DEFAULT '{}'::jsonb);

CREATE INDEX IF NOT EXISTS idx_api_ingest_run_started
    ON core.api_ingest_run (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_ingest_run_group
    ON core.api_ingest_run (group_slug, started_at DESC);

CREATE TABLE IF NOT EXISTS core.api_record (
    id              bigserial PRIMARY KEY,
    record_id       text NOT NULL,
    group_slug      text NOT NULL,
    message_type    text,
    message_name    text,
    published_at    timestamptz,
    container_count integer,
    vessel_call     text,
    summary         text,
    file_ref        text,
    media_type      text,
    size_bytes      bigint,
    checksum_sha256 text,
    stored_path     text,
    source_channel  text NOT NULL DEFAULT 'API',
    routed_service  text,
    routed_status   text,
    routed_file_id  bigint,
    ingest_run_id   bigint REFERENCES core.api_ingest_run(id),
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_api_record UNIQUE (record_id));

CREATE INDEX IF NOT EXISTS idx_api_record_group_pub
    ON core.api_record (group_slug, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_record_sha
    ON core.api_record (checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_api_record_routed
    ON core.api_record (routed_status, group_slug);

CREATE TABLE IF NOT EXISTS core.api_report_snapshot (
    id             bigserial PRIMARY KEY,
    group_slug     text NOT NULL,
    report_date    date,
    terminal       text,
    payload        jsonb NOT NULL,
    item_count     integer NOT NULL DEFAULT 0,
    payload_sha256 text NOT NULL,
    mapped_status  text NOT NULL DEFAULT 'RAW_ONLY',
    mapped_detail  jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingest_run_id  bigint REFERENCES core.api_ingest_run(id),
    fetched_at     timestamptz NOT NULL DEFAULT now());

CREATE UNIQUE INDEX IF NOT EXISTS uq_api_report_snapshot
    ON core.api_report_snapshot (group_slug,
                                 COALESCE(report_date, 'epoch'::date),
                                 COALESCE(terminal, ''),
                                 payload_sha256);
CREATE INDEX IF NOT EXISTS idx_api_report_snapshot_date
    ON core.api_report_snapshot (group_slug, report_date DESC);

CREATE TABLE IF NOT EXISTS core.api_defect_log (
    id               bigserial PRIMARY KEY,
    defect_code      text NOT NULL,
    endpoint         text,
    severity         text NOT NULL DEFAULT 'INFO',
    description      text,
    request_summary  jsonb NOT NULL DEFAULT '{}'::jsonb,
    response_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    ingest_run_id    bigint REFERENCES core.api_ingest_run(id));

CREATE INDEX IF NOT EXISTS idx_api_defect_code
    ON core.api_defect_log (defect_code, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_api_defect_observed
    ON core.api_defect_log (observed_at DESC);
"""


async def ensure_api_ingest_schema(dsn: Optional[str] = None) -> None:
    """Idempotent boot DDL for the 0124 tables (gateway lifespan)."""
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        for statement in _DDL.split(";"):
            if statement.strip():
                await conn.execute(text(statement))
    log.info("api_ingest_schema_ready")


# The upload-ledger tables a checksum may already be known to (dump imports).
# Two column conventions coexist: marine/berthing use file_hash + status, the
# rest use source_sha256 + import_status. performance has no file-level hash
# at all. The 4th element names each ledger's status column so the probe can
# ignore FAILED rows (a recorded attempt, not imported content).
_LEDGER_PROBES: Sequence[tuple[str, str, str, str]] = (
    ("core.marine_import_files", "file_hash", "marine", "status"),
    ("core.berthing_import_file", "file_hash", "berthing", "status"),
    ("core.customs_message", "source_sha256", "customs", "import_status"),
    ("core.sl_import_file", "source_sha256", "shipping_lines", "import_status"),
    ("core.cfs_ecy_import_file", "source_sha256", "cfs_ecy", "import_status"),
    ("core.gate_doc_import_file", "source_sha256", "gate_documents",
     "import_status"),
    ("core.td_import_file", "source_sha256", "transporters_drivers",
     "import_status"),
)


def payload_sha256(payload: Any) -> str:
    """Canonical-JSON sha over a report payload (the report dedup key)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SyncRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # -------------------------------------------------------------- locking
    @asynccontextmanager
    async def group_lock(self, group: str) -> AsyncIterator[bool]:
        """Per-group advisory lock (session-scoped): multi-worker uvicorn and
        manual+scheduled overlap cannot double-sync a group. Yields whether
        the lock was won; the caller skips politely when it wasn't."""
        key = f"jnpa_sync_{group}"
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            got = bool((await conn.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:k))"),
                {"k": key})).scalar())
            try:
                yield got
            finally:
                if got:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(hashtext(:k))"),
                        {"k": key})

    # ----------------------------------------------------------- sync state
    async def get_sync_state(self, group: str) -> Optional[Dict[str, Any]]:
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT group_slug, watermark_ts, last_cursor, last_run_id,"
                "       last_status, updated_at"
                " FROM core.api_sync_state WHERE group_slug = :g"),
                {"g": group})).mappings().first()
        return dict(row) if row else None

    async def list_sync_state(self) -> List[Dict[str, Any]]:
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT group_slug, watermark_ts, last_cursor, last_run_id,"
                "       last_status, updated_at"
                " FROM core.api_sync_state ORDER BY group_slug"))).mappings().all()
        return [dict(r) for r in rows]

    async def upsert_sync_state(self, group: str, *,
                                watermark_ts: Optional[datetime] = None,
                                last_cursor: Optional[str] = None,
                                last_run_id: Optional[int] = None,
                                last_status: Optional[str] = None) -> None:
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT INTO core.api_sync_state"
                " (group_slug, watermark_ts, last_cursor, last_run_id,"
                "  last_status, updated_at)"
                " VALUES (:g, :w, :c, :r, :s, now())"
                " ON CONFLICT (group_slug) DO UPDATE SET"
                "   watermark_ts = COALESCE(EXCLUDED.watermark_ts,"
                "                           core.api_sync_state.watermark_ts),"
                "   last_cursor  = EXCLUDED.last_cursor,"
                "   last_run_id  = COALESCE(EXCLUDED.last_run_id,"
                "                           core.api_sync_state.last_run_id),"
                "   last_status  = COALESCE(EXCLUDED.last_status,"
                "                           core.api_sync_state.last_status),"
                "   updated_at   = now()"),
                {"g": group, "w": watermark_ts, "c": last_cursor,
                 "r": last_run_id, "s": last_status})

    # ------------------------------------------------------------ runs
    async def open_run(self, *, trigger: str, group: Optional[str],
                       api_mode: str) -> int:
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO core.api_ingest_run (trigger, group_slug, api_mode)"
                " VALUES (:t, :g, :m) RETURNING id"),
                {"t": trigger, "g": group, "m": api_mode})).mappings().first()
        return int(row["id"])

    async def close_run(self, run_id: int, *, status: str,
                        counters: Optional[Dict[str, Any]] = None,
                        error: Optional[str] = None,
                        detail: Optional[Dict[str, Any]] = None) -> None:
        counters = counters or {}
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE core.api_ingest_run SET"
                "  finished_at = now(), status = :s, error = :e,"
                "  records_listed = :records_listed,"
                "  records_new = :records_new,"
                "  records_duplicate = :records_duplicate,"
                "  files_downloaded = :files_downloaded,"
                "  files_304 = :files_304,"
                "  files_skipped_checksum = :files_skipped_checksum,"
                "  bytes_downloaded = :bytes_downloaded,"
                "  request_count = :request_count,"
                "  rate_limit_remaining_min = :rate_limit_remaining_min,"
                "  detail = CAST(:d AS jsonb)"
                " WHERE id = :id"),
                {"id": run_id, "s": status, "e": error,
                 "records_listed": int(counters.get("records_listed", 0)),
                 "records_new": int(counters.get("records_new", 0)),
                 "records_duplicate": int(counters.get("records_duplicate", 0)),
                 "files_downloaded": int(counters.get("files_downloaded", 0)),
                 "files_304": int(counters.get("files_304", 0)),
                 "files_skipped_checksum":
                     int(counters.get("files_skipped_checksum", 0)),
                 "bytes_downloaded": int(counters.get("bytes_downloaded", 0)),
                 "request_count": int(counters.get("request_count", 0)),
                 "rate_limit_remaining_min":
                     counters.get("rate_limit_remaining_min"),
                 "d": json.dumps(detail or {}, default=str)})

    async def list_runs(self, *, limit: int = 50,
                        group: Optional[str] = None) -> List[Dict[str, Any]]:
        clause = " WHERE group_slug = :g" if group else ""
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM core.api_ingest_run" + clause +
                " ORDER BY started_at DESC LIMIT :n"),
                ({"g": group, "n": limit} if group else {"n": limit})
            )).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ records
    async def insert_record(self, *, record_id: str, group: str,
                            message_type: Optional[str],
                            message_name: Optional[str],
                            published_at: Optional[datetime],
                            container_count: Optional[int],
                            vessel_call: Optional[str],
                            summary: Optional[str],
                            file_ref: Optional[str],
                            media_type: Optional[str],
                            size_bytes: Optional[int],
                            checksum_sha256: Optional[str],
                            ingest_run_id: Optional[int],
                            payload: Dict[str, Any]) -> Optional[int]:
        """The boundary-tie absorber: ON CONFLICT (record_id) DO NOTHING.
        Returns the new row id, or None when the record was already known
        (a since=watermark-1s re-read — expected, harmless, counted)."""
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO core.api_record"
                " (record_id, group_slug, message_type, message_name,"
                "  published_at, container_count, vessel_call, summary,"
                "  file_ref, media_type, size_bytes, checksum_sha256,"
                "  ingest_run_id, payload)"
                " VALUES (:record_id, :group_slug, :message_type,"
                "  :message_name, :published_at, :container_count,"
                "  :vessel_call, :summary, :file_ref, :media_type,"
                "  :size_bytes, :checksum_sha256, :ingest_run_id,"
                "  CAST(:payload AS jsonb))"
                " ON CONFLICT (record_id) DO NOTHING RETURNING id"),
                {"record_id": record_id, "group_slug": group,
                 "message_type": message_type, "message_name": message_name,
                 "published_at": published_at,
                 "container_count": container_count,
                 "vessel_call": vessel_call, "summary": summary,
                 "file_ref": file_ref, "media_type": media_type,
                 "size_bytes": size_bytes,
                 "checksum_sha256": checksum_sha256,
                 "ingest_run_id": ingest_run_id,
                 "payload": json.dumps(payload, default=str)})).mappings().first()
        return int(row["id"]) if row else None

    async def update_record_routing(self, record_id: str, *,
                                    stored_path: Optional[str] = None,
                                    routed_service: Optional[str] = None,
                                    routed_status: Optional[str] = None,
                                    routed_file_id: Optional[int] = None) -> None:
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE core.api_record SET"
                "  stored_path = COALESCE(:p, stored_path),"
                "  routed_service = COALESCE(:s, routed_service),"
                "  routed_status = COALESCE(:st, routed_status),"
                "  routed_file_id = COALESCE(:f, routed_file_id)"
                " WHERE record_id = :r"),
                {"r": record_id, "p": stored_path, "s": routed_service,
                 "st": routed_status, "f": routed_file_id})

    async def list_records(self, *, group: Optional[str] = None,
                           routed_status: Optional[str] = None,
                           limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], {"n": limit}
        if group:
            clauses.append("group_slug = :g")
            params["g"] = group
        if routed_status:
            clauses.append("routed_status = :rs")
            params["rs"] = routed_status
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM core.api_record" + where +
                " ORDER BY published_at DESC NULLS LAST, id DESC LIMIT :n"),
                params)).mappings().all()
        return [dict(r) for r in rows]

    async def find_record_by_sha(self, sha256: str) -> Optional[Dict[str, Any]]:
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT * FROM core.api_record"
                " WHERE checksum_sha256 = :s AND stored_path IS NOT NULL"
                " LIMIT 1"), {"s": sha256})).mappings().first()
        return dict(row) if row else None

    async def known_sha(self, sha256: str) -> Optional[Dict[str, str]]:
        """Have these exact bytes already been imported VIA THE API? If so the
        download can be skipped (PDF §5.4's intended use of the checksum).

        SAME-ORIGIN scoping (LIVE/DEMO design): the probe matches only ledger
        rows with data_origin='API', so a MANUAL/dump copy of the same bytes no
        longer suppresses the API fetch — the API and manual corpora are kept
        separately tagged. The api_record store is already API-origin by
        construction, so it remains a valid skip.

        Probes tolerate missing tables/columns (partial deployments, migration
        not yet applied) — a failed probe is a miss, never an error, so the
        worst case is a redundant (still-deduped) download, never a crash."""
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            for table, column, source, status_col in _LEDGER_PROBES:
                try:
                    # A FAILED ledger row is a recorded *attempt* whose domain
                    # rows were rolled back — it must not suppress the fetch,
                    # or the record can never be repaired by a re-sync.
                    row = (await conn.execute(text(
                        f"SELECT 1 FROM {table} WHERE {column} = :s "
                        f"AND data_origin = 'API' "
                        f"AND {status_col} <> 'FAILED' LIMIT 1"),
                        {"s": sha256})).first()
                except Exception:  # noqa: BLE001 - probe, not a failure
                    continue
                if row:
                    return {"source": source, "table": table}
        record = await self.find_record_by_sha(sha256)
        if record:
            return {"source": "api_record", "table": "core.api_record"}
        return None

    # ------------------------------------------------------------ reports
    @staticmethod
    def _report_date_param(value: Optional[Union[str, _date]]) -> Optional[_date]:
        """Coerce a report date to a real ``datetime.date`` for the bind.

        ``report_date`` is threaded through the ingest layer as an ISO string
        (it doubles as the snapshot bucket key), but the column is ``date`` and
        the bind sits under ``CAST(:d AS date)`` — which makes asyncpg resolve
        the parameter's type to ``date`` and then reject a ``str`` outright
        ("'str' object has no attribute 'toordinal'"). Postgres never sees the
        statement, so the whole report group fails. Convert here, at the single
        DB boundary, so every caller is fixed at once.
        """
        if value is None or isinstance(value, _date):
            return value
        text_value = str(value).strip()
        if not text_value:
            return None
        try:
            return _date.fromisoformat(text_value[:10])
        except ValueError:
            log.warning("report_date_unparseable", report_date=text_value)
            return None

    async def insert_report_snapshot(self, *, group: str,
                                     report_date: Optional[Union[str, _date]],
                                     terminal: Optional[str],
                                     payload: Dict[str, Any],
                                     item_count: int,
                                     ingest_run_id: Optional[int]) -> Optional[int]:
        """ON CONFLICT DO NOTHING on the natural key — re-polling unchanged
        content is a no-op; returns the new snapshot id or None."""
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "INSERT INTO core.api_report_snapshot"
                " (group_slug, report_date, terminal, payload, item_count,"
                "  payload_sha256, ingest_run_id)"
                " VALUES (:g, CAST(:d AS date), :t, CAST(:p AS jsonb), :n,"
                "         :sha, :run)"
                " ON CONFLICT (group_slug, COALESCE(report_date,"
                "              'epoch'::date), COALESCE(terminal, ''),"
                "              payload_sha256)"
                " DO NOTHING RETURNING id"),
                {"g": group, "d": self._report_date_param(report_date), "t": terminal,
                 "p": json.dumps(payload, default=str), "n": item_count,
                 "sha": payload_sha256(payload),
                 "run": ingest_run_id})).mappings().first()
        return int(row["id"]) if row else None

    async def update_report_mapped(self, snapshot_id: int, *, status: str,
                                   detail: Optional[Dict[str, Any]] = None) -> None:
        engine = get_engine(self._dsn)
        async with engine.begin() as conn:
            await conn.execute(text(
                "UPDATE core.api_report_snapshot SET mapped_status = :s,"
                " mapped_detail = CAST(:d AS jsonb) WHERE id = :id"),
                {"id": snapshot_id, "s": status,
                 "d": json.dumps(detail or {}, default=str)})

    async def list_report_snapshots(self, *, group: Optional[str] = None,
                                    limit: int = 100) -> List[Dict[str, Any]]:
        clause = " WHERE group_slug = :g" if group else ""
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM core.api_report_snapshot" + clause +
                " ORDER BY report_date DESC NULLS LAST, id DESC LIMIT :n"),
                ({"g": group, "n": limit} if group else {"n": limit})
            )).mappings().all()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ defects
    async def log_defects(self, observations: Sequence[Any],
                          run_id: Optional[int]) -> int:
        """Persist drained client DefectObservations. Best-effort — defect
        logging must never fail a sync run."""
        if not observations:
            return 0
        engine = get_engine(self._dsn)
        written = 0
        async with engine.begin() as conn:
            for obs in observations:
                try:
                    await conn.execute(text(
                        "INSERT INTO core.api_defect_log"
                        " (defect_code, endpoint, severity, description,"
                        "  observed_at, ingest_run_id)"
                        " VALUES (:c, :e, :s, :d,"
                        "         COALESCE(CAST(:o AS timestamptz), now()),"
                        "         :r)"),
                        {"c": getattr(obs, "code", "UNKNOWN"),
                         "e": getattr(obs, "endpoint", None),
                         "s": getattr(obs, "severity", "INFO"),
                         "d": getattr(obs, "detail", None),
                         # DefectObservation.observed_at is an ISO *string*, but
                         # asyncpg types :o from the CAST and rejects a str
                         # outright ("expected a datetime.date or datetime
                         # .datetime instance"). Every observation therefore
                         # failed to persist — and these rows are a deliverable
                         # (JNPA's 31-Jul notice requires observed defects to be
                         # reported), so the loss was silent but not harmless.
                         "o": _as_datetime(getattr(obs, "observed_at", None)),
                         "r": run_id})
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("api_defect_log_failed", error=str(exc))
        return written

    async def list_defects(self, *, limit: int = 200,
                           severity: Optional[str] = None) -> List[Dict[str, Any]]:
        clause = " WHERE severity = :sev" if severity else ""
        engine = get_engine(self._dsn)
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT * FROM core.api_defect_log" + clause +
                " ORDER BY observed_at DESC LIMIT :n"),
                ({"sev": severity, "n": limit} if severity else {"n": limit})
            )).mappings().all()
        return [dict(r) for r in rows]


__all__ = ["SyncRepository", "ensure_api_ingest_schema", "payload_sha256"]
