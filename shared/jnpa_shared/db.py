"""SQLAlchemy 2.0 async engine factory + tiny CRUD helpers.

The engine targets Postgres/TimescaleDB via asyncpg. Services that want the
ORM can build on `get_engine()` / `get_sessionmaker()`; the lightweight
`fetch_all` / `fetch_one` / `execute` helpers cover the simple read/write
paths (e.g. the bootstrap self-test) without defining ORM models.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import get_settings


# Registry of live engines so dispose_all() can close them cleanly.
_ENGINES: list[AsyncEngine] = []


@lru_cache(maxsize=4)
def get_engine(dsn: Optional[str] = None, echo: bool = False) -> AsyncEngine:
    """Return a cached async engine for the given DSN (defaults to settings)."""
    dsn = dsn or get_settings().postgres_dsn
    engine = create_async_engine(
        dsn,
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
    )
    _ENGINES.append(engine)
    return engine


@lru_cache(maxsize=4)
def get_sessionmaker(dsn: Optional[str] = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(dsn), expire_on_commit=False)


async def fetch_all(
    sql: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    dsn: Optional[str] = None,
) -> Sequence[Mapping[str, Any]]:
    """Run a SELECT and return rows as a list of dict-like mappings."""
    engine = get_engine(dsn)
    async with engine.connect() as conn:
        result = await conn.execute(text(sql), params or {})
        return [dict(r) for r in result.mappings().all()]


async def fetch_one(
    sql: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    dsn: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    rows = await fetch_all(sql, params, dsn=dsn)
    return rows[0] if rows else None


async def execute(
    sql: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    dsn: Optional[str] = None,
) -> int:
    """Run an INSERT/UPDATE/DELETE inside a transaction. Returns rowcount."""
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        return result.rowcount if result.rowcount is not None else 0


async def execute_returning(
    sql: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    dsn: Optional[str] = None,
) -> Optional[Mapping[str, Any]]:
    """Run a WRITING statement (``INSERT/UPDATE/DELETE ... RETURNING``) inside a
    COMMITTED transaction and return the first row (dict-like) or ``None``.

    Use this — NOT :func:`fetch_one` — for statements that modify data and need a
    value back (e.g. a ``RETURNING id``). :func:`fetch_one` runs on a
    non-committing ``engine.connect()``: that is correct for a ``SELECT`` but a
    plain connection opens an implicit transaction that is **rolled back on close**,
    so an ``INSERT ... RETURNING`` would hand back an id yet never persist the row.
    This helper uses ``engine.begin()`` so the write commits, exactly like
    :func:`execute`.
    """
    engine = get_engine(dsn)
    async with engine.begin() as conn:
        result = await conn.execute(text(sql), params or {})
        row = result.mappings().first()
        return dict(row) if row is not None else None


async def insert_row(
    table: str,
    row: Mapping[str, Any],
    *,
    dsn: Optional[str] = None,
) -> int:
    """Generic single-row INSERT. `table` may be schema-qualified ('jnpa.x')."""
    cols = list(row.keys())
    col_list = ", ".join(cols)
    placeholders = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
    return await execute(sql, row, dsn=dsn)


async def ping(*, dsn: Optional[str] = None) -> bool:
    """Return True if `SELECT 1` succeeds."""
    row = await fetch_one("SELECT 1 AS ok", dsn=dsn)
    return bool(row and row.get("ok") == 1)


async def dispose_all() -> None:
    """Dispose every live engine (call on shutdown / between tests)."""
    for engine in _ENGINES:
        await engine.dispose()
    _ENGINES.clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


__all__ = [
    "get_engine",
    "get_sessionmaker",
    "fetch_all",
    "fetch_one",
    "execute",
    "execute_returning",
    "insert_row",
    "ping",
    "dispose_all",
    "AsyncSession",
]
