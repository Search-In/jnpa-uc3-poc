"""CFS-ECY CODECO persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to ``core.cfs_ecy_movement`` (+ the derived
``mart.v_cfs_ecy_dwell`` view). It performs no business logic and no HTTP; it just
runs parameterised statements through the cached SQLAlchemy async engine
(``jnpa_shared.db.get_engine``) exactly like :mod:`services.cargo.repository` —
reads on a plain ``connect()``. No ORM.

Read-only wrt every EXISTING table: the one cross-module read is a soft lookup of
``core.cargo.lifecycle_status`` by container_number (no write, no FK), used to
enrich a container timeline with its lifecycle state when the container is also
tracked by the Container Lifecycle module.

Injection-safe by construction: filter COLUMN names are fixed identifiers from a
whitelist, never interpolated from client input; every VALUE is a bound parameter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cfs_ecy.repository")

_COLUMNS = (
    "id", "facility_type", "container_number", "iso_valid",
    "event_ts", "mode", "source", "source_file", "created_at",
)
_SELECT_COLS = ", ".join(f"m.{c}" for c in _COLUMNS)

# Whitelisted equality filters (keys fixed, values bound → injection-safe).
_EQ_FILTERS = ("facility_type", "mode")
# Whitelisted sort columns.
_SORTS = {"event_ts": "m.event_ts", "container_number": "m.container_number",
          "facility_type": "m.facility_type", "mode": "m.mode", "id": "m.id"}


class CfsEcyRepository:
    """Raw-SQL reads for ``core.cfs_ecy_movement``. Stateless apart from the DSN,
    so a single instance is safe to share across requests (engine + pool cached)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------- filters
    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"m.{col} = :{col}")
                params[col] = val
        # container: case-insensitive contains search
        container = filters.get("container")
        if container:
            conds.append("m.container_number ILIKE :container")
            params["container"] = f"%{str(container).strip()}%"
        # event_ts date range
        if filters.get("ts_from") is not None:
            conds.append("m.event_ts >= :ts_from")
            params["ts_from"] = filters["ts_from"]
        if filters.get("ts_to") is not None:
            conds.append("m.event_ts <= :ts_to")
            params["ts_to"] = filters["ts_to"]
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    # ------------------------------------------------------------- list + count
    async def list_movements(self, filters: Mapping[str, Any], *, sort: str,
                             direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "m.event_ts")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_SELECT_COLS} FROM core.cfs_ecy_movement m {clause} "
            f"ORDER BY {order_col} {order_dir}, m.id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        sql = f"SELECT count(*) AS n FROM core.cfs_ecy_movement m {clause}"
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------- stats
    async def stats(self, filters: Mapping[str, Any]) -> dict:
        """Movement totals, distinct container count, and net-in ('active') count,
        scoped by the same filters as the list. Dwell is computed separately
        (CFS-only) in :meth:`dwell_summary`."""
        clause, params = self._where(filters)
        sql = (
            "SELECT "
            "  count(*) FILTER (WHERE m.mode='IN')  AS total_in, "
            "  count(*) FILTER (WHERE m.mode='OUT') AS total_out, "
            "  count(DISTINCT m.container_number)   AS container_count, "
            "  count(*)                             AS total_events, "
            "  count(*) FILTER (WHERE m.iso_valid = false) AS iso_invalid "
            f"FROM core.cfs_ecy_movement m {clause}"
        )
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
            # 'active' containers = still inside a facility (net IN > OUT).
            active_sql = (
                "SELECT count(*) AS n FROM ("
                "  SELECT m.container_number, m.facility_type "
                f"  FROM core.cfs_ecy_movement m {clause} "
                "  GROUP BY m.container_number, m.facility_type "
                "  HAVING count(*) FILTER (WHERE m.mode='IN') > "
                "         count(*) FILTER (WHERE m.mode='OUT')"
                ") s"
            )
            active_row = (await conn.execute(text(active_sql), params)).mappings().first()
        r = dict(row) if row else {}
        return {
            "total_in": int(r.get("total_in") or 0),
            "total_out": int(r.get("total_out") or 0),
            "container_count": int(r.get("container_count") or 0),
            "total_events": int(r.get("total_events") or 0),
            "iso_invalid": int(r.get("iso_invalid") or 0),
            "active_containers": int(active_row["n"]) if active_row else 0,
        }

    async def dwell_summary(self, filters: Mapping[str, Any]) -> dict:
        """Average + median CFS dwell (hours) over containers that have a computed
        dwell in the view. ECY dwell is NULL by design, so it is naturally excluded.
        Filters restrict by facility/date via the underlying movement rows."""
        # Restrict the view to CFS + optional facility filter. Dwell is CFS-only.
        params: dict[str, Any] = {}
        conds = ["d.dwell_hours IS NOT NULL"]
        facility = filters.get("facility_type")
        if facility and facility != "CFS":
            # A non-CFS facility filter yields no dwell rows (ECY has none).
            return {"average_dwell_hours": None, "median_dwell_hours": None, "dwell_count": 0}
        sql = (
            "SELECT round(avg(d.dwell_hours)::numeric, 2) AS average_dwell_hours, "
            "  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY d.dwell_hours)::numeric, 2) "
            "    AS median_dwell_hours, "
            "  count(*) AS dwell_count "
            "FROM mart.v_cfs_ecy_dwell d "
            f"WHERE {' AND '.join(conds)}"
        )
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(sql), params)).mappings().first()
        r = dict(row) if row else {}
        return {
            "average_dwell_hours": float(r["average_dwell_hours"]) if r.get("average_dwell_hours") is not None else None,
            "median_dwell_hours": float(r["median_dwell_hours"]) if r.get("median_dwell_hours") is not None else None,
            "dwell_count": int(r.get("dwell_count") or 0),
        }

    async def daily_throughput(self, filters: Mapping[str, Any]) -> list[dict]:
        """Per-day IN/OUT counts (IST calendar day), oldest-first, for the trend chart."""
        clause, params = self._where(filters)
        sql = (
            "SELECT (m.event_ts AT TIME ZONE 'Asia/Kolkata')::date AS day, "
            "  count(*) FILTER (WHERE m.mode='IN')  AS in_count, "
            "  count(*) FILTER (WHERE m.mode='OUT') AS out_count "
            f"FROM core.cfs_ecy_movement m {clause} "
            "GROUP BY day ORDER BY day ASC"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [{"day": str(r["day"]), "in_count": int(r["in_count"]),
                     "out_count": int(r["out_count"])}
                    for r in result.mappings().all()]

    # ------------------------------------------------------------- timeline
    async def container_events(self, container_number: str) -> list[dict]:
        """All CODECO gate events for one container, across BOTH facilities,
        chronological (oldest-first)."""
        sql = (
            f"SELECT {_SELECT_COLS} FROM core.cfs_ecy_movement m "
            "WHERE m.container_number = :cn ORDER BY m.event_ts ASC, m.id ASC"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"cn": container_number})
            return [dict(r) for r in result.mappings().all()]

    async def container_dwell(self, container_number: str) -> list[dict]:
        """Per-facility dwell view rows for one container (CFS dwell only)."""
        sql = (
            "SELECT container_number, facility_type, first_in_ts, last_out_ts, "
            "  in_events, out_events, dwell_hours "
            "FROM mart.v_cfs_ecy_dwell WHERE container_number = :cn "
            "ORDER BY facility_type"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), {"cn": container_number})
            return [dict(r) for r in result.mappings().all()]

    async def cargo_lifecycle(self, container_number: str) -> Optional[dict]:
        """Soft, read-only lookup of the container in the EXISTING Container
        Lifecycle module (core.cargo). Returns None when the container is not
        tracked there (the common case for pure off-dock CODECO events). Degrades
        gracefully if the cargo table is absent (returns None)."""
        sql = (
            "SELECT container_number, lifecycle_status, customs_status, "
            "  yard_block, is_released, vessel_name, vehicle_number, updated_at "
            "FROM core.cargo WHERE container_number = :cn"
        )
        try:
            async with get_engine(self._dsn).connect() as conn:
                row = (await conn.execute(text(sql), {"cn": container_number})).mappings().first()
            return dict(row) if row else None
        except Exception as exc:  # noqa: BLE001 — cargo table missing / read blip
            log.warning("cfs_ecy.cargo_lookup_failed", extra={"error": str(exc)})
            return None

    # ------------------------------------------------------------- dwell report
    async def dwell_report(self, filters: Mapping[str, Any], *, limit: int,
                           offset: int) -> tuple[list[dict], int]:
        """CFS dwell report: one row per CFS container with a computed dwell,
        longest-dwell-first. Returns (rows, total)."""
        params: dict[str, Any] = {}
        conds = ["d.facility_type = 'CFS'", "d.dwell_hours IS NOT NULL"]
        if filters.get("ts_from") is not None:
            conds.append("d.first_in_ts >= :ts_from")
            params["ts_from"] = filters["ts_from"]
        if filters.get("ts_to") is not None:
            conds.append("d.last_out_ts <= :ts_to")
            params["ts_to"] = filters["ts_to"]
        where = " AND ".join(conds)
        async with get_engine(self._dsn).connect() as conn:
            total_row = (await conn.execute(
                text(f"SELECT count(*) AS n FROM mart.v_cfs_ecy_dwell d WHERE {where}"),
                params)).mappings().first()
            params.update({"limit": limit, "offset": offset})
            rows = (await conn.execute(text(
                "SELECT d.container_number, d.facility_type, d.first_in_ts, "
                "  d.last_out_ts, d.in_events, d.out_events, d.dwell_hours "
                f"FROM mart.v_cfs_ecy_dwell d WHERE {where} "
                "ORDER BY d.dwell_hours DESC NULLS LAST, d.container_number ASC "
                "LIMIT :limit OFFSET :offset"), params)).mappings().all()
        return [dict(r) for r in rows], (int(total_row["n"]) if total_row else 0)

    # ================================================================ Data Upload
    # Persistence + import-ledger for the reusable Data-Upload sub-module (migration
    # 0034). Writes ONLY the new ledger tables (cfs_ecy_import_files /
    # cfs_ecy_import_errors) and inserts movement rows through the SAME
    # (facility_type, container_number, event_ts, mode) UNIQUE key with ON CONFLICT
    # DO NOTHING — idempotent, duplicate-safe, and it NEVER overwrites an existing row.

    async def find_file_by_sha(self, sha256: str) -> Optional[dict]:
        """The prior ledger row for identical bytes (content-level dedup), or None."""
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, facility_type, source_file, import_status, record_count, "
                "imported_count, error_count, duplicate_count, created_at "
                "FROM core.cfs_ecy_import_file WHERE source_sha256 = :sha"),
                {"sha": sha256})).mappings().first()
        return dict(row) if row else None

    async def persist(self, records: Sequence[Mapping[str, Any]], *, facility_type: str,
                      source_file: str, source_sha256: str, physical_format: str,
                      file_size: Optional[int] = None, uploaded_by: Optional[str] = None,
                      source: str = "UPLOAD") -> dict:
        """Persist one uploaded CFS-ECY file atomically + idempotently.

        The ledger row, every movement insert and the final status update run in ONE
        transaction. Re-uploading identical bytes is a no-op (SKIPPED_DUPLICATE); rows
        that collide with an EXISTING movement are silently skipped (counted as
        duplicates) and never overwrite it. Returns the outcome envelope."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return {"file_id": existing["id"], "import_status": "SKIPPED_DUPLICATE",
                    "record_count": existing["record_count"],
                    "imported_count": existing["imported_count"],
                    "error_count": existing["error_count"],
                    "duplicate_count": existing["duplicate_count"], "duplicate": True}

        envelope = {
            "facility_type": facility_type, "physical_format": physical_format,
            "source_file": source_file, "source_sha256": source_sha256,
            "file_size_bytes": file_size, "record_count": len(records),
            "uploaded_by": uploaded_by, "source": source,
        }
        rows = [{"facility_type": r["facility_type"], "container_number": r["container_number"],
                 "iso_valid": bool(r["iso_valid"]), "event_ts": r["event_ts"],
                 "mode": r["mode"], "source": r.get("source") or "UPLOAD",
                 "source_file": r.get("source_file") or source_file}
                for r in records]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT), envelope)).mappings().first()["id"]
                imported = 0
                if rows:
                    for r in rows:
                        r["import_file_id"] = fid
                    await conn.execute(text(_MOVEMENT_INSERT), rows)
                    imported = int((await conn.execute(text(
                        "SELECT count(*) FROM core.cfs_ecy_movement WHERE import_file_id = :id"),
                        {"id": fid})).scalar() or 0)
                dup = len(records) - imported
                await conn.execute(text(
                    "UPDATE core.cfs_ecy_import_file SET import_status = 'SUCCESS', "
                    "imported_count = :imp, duplicate_count = :dup, error_count = 0, "
                    "updated_at = now() WHERE id = :id"),
                    {"imp": imported, "dup": dup, "id": fid})
            return {"file_id": fid, "import_status": "SUCCESS", "record_count": len(records),
                    "imported_count": imported, "error_count": 0,
                    "duplicate_count": dup, "duplicate": False}
        except IntegrityError as exc:
            dup_row = await self.find_file_by_sha(source_sha256)
            if dup_row is not None:
                return {"file_id": dup_row["id"], "import_status": "SKIPPED_DUPLICATE",
                        "record_count": dup_row["record_count"],
                        "imported_count": dup_row["imported_count"],
                        "error_count": dup_row["error_count"],
                        "duplicate_count": dup_row["duplicate_count"], "duplicate": True}
            return await self._record_failure(envelope, str(getattr(exc, "orig", exc)))
        except Exception as exc:  # noqa: BLE001 — record + surface as FAILED, never partial
            log.warning("cfs_ecy.persist_failed", extra={"source_file": source_file,
                                                         "error": str(exc)})
            return await self._record_failure(envelope, str(exc))

    async def _record_failure(self, envelope: Mapping[str, Any], detail: str) -> dict:
        row = dict(envelope)
        row["error_detail"] = detail[:4000]
        try:
            async with get_engine(self._dsn).begin() as conn:
                fid = (await conn.execute(text(_FILE_INSERT_FAILED), row)).mappings().first()["id"]
                await conn.execute(text(
                    "INSERT INTO core.cfs_ecy_import_error (import_file_id, record_ref, "
                    "error_code, error_detail) VALUES (:fid, NULL, 'PERSIST_FAILED', :d)"),
                    {"fid": fid, "d": detail[:4000]})
            fail_id: Optional[int] = fid
        except Exception as exc:  # noqa: BLE001
            log.error("cfs_ecy.failure_record_failed", extra={"error": str(exc)})
            fail_id = None
        return {"file_id": fail_id, "import_status": "FAILED",
                "record_count": envelope["record_count"], "imported_count": 0,
                "error_count": 1, "duplicate_count": 0, "duplicate": False}

    async def record_rejected_upload(self, *, facility_type: Optional[str],
                                     physical_format: str, source_file: str,
                                     source_sha256: str, file_size: Optional[int],
                                     uploaded_by: Optional[str], detail: str,
                                     errors: Sequence[Mapping[str, Any]]) -> Optional[int]:
        """Record a structurally-rejected upload (e.g. missing required columns / no
        valid rows) as a FAILED ledger row so it appears in upload history, with its
        column/row errors. Writes NO movement rows. De-dupes on sha256."""
        existing = await self.find_file_by_sha(source_sha256)
        if existing is not None:
            return existing["id"]
        envelope = {
            "facility_type": facility_type, "physical_format": physical_format,
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
            log.warning("cfs_ecy.reject_record_failed", extra={"error": str(exc)})
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
                "INSERT INTO core.cfs_ecy_import_error (import_file_id, record_ref, "
                "error_code, error_detail) VALUES (:fid, :ref, :code, :detail)"), rows)

    async def mark_partial(self, file_id: int, *, error_count: int) -> None:
        """Flip a successful import to PARTIAL when some source rows were skipped as
        invalid (records the honest outcome; the valid rows are already persisted)."""
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(text(
                "UPDATE core.cfs_ecy_import_file SET import_status = 'PARTIAL', "
                "error_count = :n, updated_at = now() WHERE id = :id"),
                {"n": error_count, "id": file_id})

    # ------------------------------------------------------------- ledger reads
    @staticmethod
    def _file_where(filters: Mapping[str, Any]) -> tuple[str, dict]:
        clauses, params = [], {}
        for col in ("facility_type", "import_status", "source"):
            if filters.get(col) is not None:
                clauses.append(f"{col} = :{col}")
                params[col] = filters[col]
        return ((" WHERE " + " AND ".join(clauses)) if clauses else ""), params

    async def list_files(self, *, filters: Mapping[str, Any], limit: int, offset: int) -> list[dict]:
        where, params = self._file_where(filters)
        params.update(limit=limit, offset=offset)
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, facility_type, physical_format, source_file, record_count, "
                "imported_count, error_count, duplicate_count, import_status, error_detail, "
                "uploaded_by, source, created_at, updated_at "
                f"FROM core.cfs_ecy_import_file{where} "
                "ORDER BY id DESC LIMIT :limit OFFSET :offset"), params)
            return [dict(r) for r in res.mappings().all()]

    async def count_files(self, *, filters: Mapping[str, Any]) -> int:
        where, params = self._file_where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.cfs_ecy_import_file{where}"), params)).scalar() or 0)

    async def get_file(self, file_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT id, facility_type, physical_format, source_file, source_sha256, "
                "file_size_bytes, record_count, imported_count, error_count, duplicate_count, "
                "import_status, error_detail, uploaded_by, source, created_at, updated_at "
                "FROM core.cfs_ecy_import_file WHERE id = :id"), {"id": file_id})).mappings().first()
        return dict(row) if row else None

    async def list_file_errors(self, file_id: int, *, limit: int, offset: int) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(
                "SELECT id, record_ref, error_code, error_detail, created_at "
                "FROM core.cfs_ecy_import_error WHERE import_file_id = :id "
                "ORDER BY id LIMIT :limit OFFSET :offset"),
                {"id": file_id, "limit": limit, "offset": offset})
            return [dict(r) for r in res.mappings().all()]


# --------------------------------------------------------------------------- SQL
_FILE_INSERT = """
INSERT INTO core.cfs_ecy_import_file
    (facility_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, uploaded_by, source)
VALUES
    (:facility_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, 'PENDING', :uploaded_by, :source)
RETURNING id
"""

_FILE_INSERT_FAILED = """
INSERT INTO core.cfs_ecy_import_file
    (facility_type, physical_format, source_file, source_sha256, file_size_bytes,
     record_count, import_status, error_detail, uploaded_by, source)
VALUES
    (:facility_type, :physical_format, :source_file, :source_sha256, :file_size_bytes,
     :record_count, 'FAILED', :error_detail, :uploaded_by, :source)
RETURNING id
"""

_MOVEMENT_INSERT = """
INSERT INTO core.cfs_ecy_movement
    (facility_type, container_number, iso_valid, event_ts, mode, source, source_file,
     import_file_id)
VALUES
    (:facility_type, :container_number, :iso_valid, :event_ts, :mode, :source, :source_file,
     :import_file_id)
ON CONFLICT ON CONSTRAINT uq_cfs_ecy_movement DO NOTHING
"""
