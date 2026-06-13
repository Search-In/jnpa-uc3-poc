"""Redis cache layer for the gateway.

Every successful upstream response the orchestrator makes is written to Redis
with an appropriate TTL so the CACHED fallback rung can replay the last good
answer when the upstream goes dark.

Cache-key convention (spec):

    jnpa:cache:{api}:{key}        e.g. jnpa:cache:vahan:MH04AB1234

Values are wrapped with the timestamp they were written so the dashboard /
debug surfaces can show cache age, and the CACHED decision can report how stale
the served value is.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from jnpa_shared import redis_io

from .logging import get_logger

log = get_logger("gateway.cache")

PREFIX = "jnpa:cache"


def cache_key(api: str, key: str) -> str:
    """Build the canonical cache key, e.g. ``jnpa:cache:vahan:MH04AB1234``."""
    return f"{PREFIX}:{api}:{key}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def put(api: str, key: str, value: Any, ttl: int) -> None:
    """Store a successful upstream response under ``jnpa:cache:{api}:{key}``.

    Best-effort: a Redis outage must never fail the request the gateway is
    serving — we just skip caching and log it.
    """
    wrapped = {"cached_at": _now_iso(), "value": value}
    try:
        await redis_io.cache_set(cache_key(api, key), wrapped, ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("cache_put_failed", api=api, key=key, error=str(exc))


async def get(api: str, key: str) -> Optional[dict]:
    """Return ``{"value": ..., "cached_at": iso, "age_s": float}`` or None.

    ``None`` means a true cache miss (no value, or Redis unreachable) — the
    orchestrator then drops to the next fallback rung (e.g. PROVISIONAL).
    """
    try:
        raw = await redis_io.cache_get(cache_key(api, key))
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("cache_get_failed", api=api, key=key, error=str(exc))
        return None
    if raw is None:
        return None
    # Tolerate both the wrapped shape and a bare value (defensive).
    if isinstance(raw, dict) and "value" in raw and "cached_at" in raw:
        age = _age_seconds(raw["cached_at"])
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": age}
    return {"value": raw, "cached_at": None, "age_s": None}


def _age_seconds(cached_at: Optional[str]) -> Optional[float]:
    if not cached_at:
        return None
    try:
        then = datetime.fromisoformat(cached_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(tz=timezone.utc) - then).total_seconds()
    except (ValueError, TypeError):
        return None


__all__ = ["cache_key", "put", "get", "PREFIX"]
