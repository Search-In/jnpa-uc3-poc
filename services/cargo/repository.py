"""Cargo persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to ``jnpa.cargo``. It performs no business logic
and no HTTP; it just runs parameterised statements through the cached
SQLAlchemy async engine (``jnpa_shared.db.get_engine``) exactly like
:mod:`services.fastag.service` — reads on a plain ``connect()``, writes inside a
single ``engine.begin()`` transaction (auto-commit / auto-rollback). No ORM.

Errors are surfaced as typed exceptions (:class:`CargoConflict`) so the service
layer can map them to HTTP status codes without importing SQLAlchemy.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.cargo.repository")


class CargoConflict(Exception):
    """Raised when an INSERT violates the container_number primary key."""


class CargoNotFound(Exception):
    """Raised when an update/delete targets a container_number that is absent."""


# Every column the API round-trips, in a stable order. `created_at`/`updated_at`
# are server-managed (DEFAULT now() + the BEFORE-UPDATE trigger) and never set by
# a client write.
_COLUMNS = (
    "container_number", "vessel_name", "customs_status", "yard_block",
    "is_released", "vehicle_number", "gate", "camera_id", "eta",
    "created_at", "updated_at",
)
_SELECT_COLS = ", ".join(_COLUMNS)

# Columns a client may set on create / patch on update (server-managed audit
# columns and the immutable PK are deliberately excluded from the update set).
_WRITABLE = (
    "vessel_name", "customs_status", "yard_block", "is_released",
    "vehicle_number", "gate", "camera_id", "eta",
)

_INSERT = f"""
INSERT INTO jnpa.cargo
    (container_number, vessel_name, customs_status, yard_block, is_released,
     vehicle_number, gate, camera_id, eta)
VALUES
    (:container_number, :vessel_name, :customs_status, :yard_block, :is_released,
     :vehicle_number, :gate, :camera_id, :eta)
RETURNING {_SELECT_COLS}
"""

_SELECT_ONE = f"SELECT {_SELECT_COLS} FROM jnpa.cargo WHERE container_number = :container_number"

_DELETE = "DELETE FROM jnpa.cargo WHERE container_number = :container_number"


class CargoRepository:
    """Raw-SQL CRUD for ``jnpa.cargo``. Stateless apart from the DSN, so a single
    instance is safe to share across requests (the engine + pool are cached)."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ create
    async def create(self, row: Mapping[str, Any]) -> dict:
        """INSERT one cargo row and return it. Raises :class:`CargoConflict` if
        the container_number already exists (PK violation)."""
        params = {c: row.get(c) for c in
                  ("container_number", *_WRITABLE)}
        try:
            async with get_engine(self._dsn).begin() as conn:
                result = await conn.execute(text(_INSERT), params)
                created = result.mappings().first()
        except IntegrityError as exc:  # unique_violation on the PK
            raise CargoConflict(str(getattr(exc, "orig", exc))) from exc
        return dict(created) if created else dict(params)

    # -------------------------------------------------------------------- read
    async def get(self, container_number: str) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(_SELECT_ONE),
                                        {"container_number": container_number})
            row = result.mappings().first()
        return dict(row) if row else None

    # Column names allowed as equality filters. Keys are fixed identifiers (NEVER
    # interpolated from client input); values are always bound parameters — so the
    # WHERE clause is injection-safe by construction.
    _FILTER_COLS = (
        "container_number", "customs_status", "yard_block", "is_released",
        "vehicle_number",
    )

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """Build a parameterised ``WHERE`` clause from the whitelisted filters that
        are actually provided (non-None). Shared by list() and count()."""
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in self._FILTER_COLS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"{col} = :{col}")
                params[col] = val
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list(
        self,
        *,
        container_number: Optional[str] = None,
        customs_status: Optional[str] = None,
        yard_block: Optional[str] = None,
        is_released: Optional[bool] = None,
        vehicle_number: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """List cargo, newest ETA first. Every filter is an optional equality
        match, applied only when provided — so the no-arg call is unchanged
        (backward compatible)."""
        clause, params = self._where(locals())
        params.update({"limit": limit, "offset": offset})
        sql = (
            f"SELECT {_SELECT_COLS} FROM jnpa.cargo {clause} "
            "ORDER BY eta DESC NULLS LAST, created_at DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            return [dict(r) for r in result.mappings().all()]

    async def count(
        self,
        *,
        container_number: Optional[str] = None,
        customs_status: Optional[str] = None,
        yard_block: Optional[str] = None,
        is_released: Optional[bool] = None,
        vehicle_number: Optional[str] = None,
    ) -> int:
        """Total rows matching the same filters as list() (ignores limit/offset).
        Powers the X-Total-Count header so a paginated UI knows the full size."""
        clause, params = self._where(locals())
        sql = f"SELECT count(*) AS n FROM jnpa.cargo {clause}"
        async with get_engine(self._dsn).connect() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ update
    async def update(self, container_number: str, fields: Mapping[str, Any]) -> dict:
        """Patch the writable columns supplied in ``fields`` (updated_at is set by
        the DB trigger). Returns the full updated row. Raises
        :class:`CargoNotFound` if no such container exists."""
        patch = {k: v for k, v in fields.items() if k in _WRITABLE}
        if not patch:  # nothing to change — behave as a read (still 404 if absent)
            existing = await self.get(container_number)
            if existing is None:
                raise CargoNotFound(container_number)
            return existing
        set_clause = ", ".join(f"{k} = :{k}" for k in patch)
        params = {**patch, "container_number": container_number}
        sql = (
            f"UPDATE jnpa.cargo SET {set_clause} "
            f"WHERE container_number = :container_number RETURNING {_SELECT_COLS}"
        )
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(sql), params)
            row = result.mappings().first()
        if row is None:
            raise CargoNotFound(container_number)
        return dict(row)

    # ------------------------------------------------------------------ delete
    async def delete(self, container_number: str) -> bool:
        """DELETE one cargo row. Returns True if a row was removed, else False."""
        async with get_engine(self._dsn).begin() as conn:
            result = await conn.execute(text(_DELETE),
                                        {"container_number": container_number})
            return bool(result.rowcount)
