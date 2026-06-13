"""Async Redis client + TTL cache helpers.

Uses redis-py's asyncio client (the modern replacement for aioredis, which is
now merged into redis-py). JSON-encodes values on the way in and decodes on the
way out so callers can cache dicts/lists directly.
"""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Optional

import redis.asyncio as redis

from .config import get_settings

DEFAULT_TTL = 300  # seconds

# Registry of live clients so close() can shut them down cleanly.
_CLIENTS: list["redis.Redis"] = []


@lru_cache(maxsize=4)
def get_client(url: Optional[str] = None) -> "redis.Redis":
    """Return a cached async Redis client (decodes responses to str)."""
    url = url or get_settings().redis_url
    client = redis.from_url(url, encoding="utf-8", decode_responses=True)
    _CLIENTS.append(client)
    return client


async def cache_set(key: str, value: Any, ttl: int = DEFAULT_TTL) -> None:
    """JSON-encode and store `value` under `key` with a TTL (seconds)."""
    client = get_client()
    await client.set(key, json.dumps(value, separators=(",", ":")), ex=ttl)


async def cache_get(key: str) -> Optional[Any]:
    """Return the JSON-decoded value for `key`, or None if absent."""
    client = get_client()
    raw = await client.get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


async def cache_delete(key: str) -> int:
    client = get_client()
    return await client.delete(key)


async def ping(url: Optional[str] = None) -> bool:
    """Return True if the Redis server answers PING."""
    client = get_client(url)
    return bool(await client.ping())


async def close() -> None:
    """Close all live clients (call on shutdown / between tests)."""
    for client in _CLIENTS:
        await client.aclose()
    _CLIENTS.clear()
    get_client.cache_clear()


__all__ = [
    "get_client",
    "cache_set",
    "cache_get",
    "cache_delete",
    "ping",
    "close",
    "DEFAULT_TTL",
]
