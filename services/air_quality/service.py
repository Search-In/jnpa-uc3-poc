"""Air-quality service — OpenAQ latest readings with graceful fallback.

The single entry point the /api/air-quality/current router calls. Thin over
:class:`integrations.openaq.OpenAQClient` (HTTP) and
:class:`services.air_quality.repository.AirQualityRepository` (persistence),
in the same mould as services.traffic.service: stateless apart from config.

Fallback chain (mirroring the traffic service's vocabulary — a provider
outage must NEVER break the API):

    LIVE       -> fresh fetch (nearest stations + newest sensor values)
    CACHED     -> last good answer from Redis
                  (key jnpa:cache:openaq:{lat}:{lon})
    DATABASE   -> the last persisted core.air_quality_readings row for the
                  coordinate (the audit trail doubles as the third rung)
    SYNTHETIC  -> deterministic port-air floor, clearly tagged

Response contract (source metadata always attached):
    status  LIVE      — fresh from OpenAQ
            DEGRADED  — served from the cache or database rung
            OFFLINE   — nothing real available, synthetic floor
    source  OPENAQ | OPENAQ_CACHE | OPENAQ_DB | SYNTHETIC

A LIVE answer is written back to Redis + core.air_quality_readings
(best-effort — an infra blip never fails the request being served).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from jnpa_shared import redis_io
from jnpa_shared.logging import get_logger

from integrations.openaq import OpenAQClient, OpenAQError, POLLUTANTS

from .repository import AirQualityRepository

log = get_logger("services.air_quality.service")

# Cache-key convention matches gateway/cache.py: jnpa:cache:{api}:{key}.
CACHE_PREFIX = "jnpa:cache:openaq"
# OpenAQ stations report hourly at best — a 5-minute cache is plenty fresh.
DEFAULT_CACHE_TTL_S = 300

PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_DATABASE = "DATABASE"
PATH_SYNTHETIC = "SYNTHETIC"
_PATH_SOURCE = {PATH_LIVE: "OPENAQ", PATH_CACHED: "OPENAQ_CACHE",
                PATH_DATABASE: "OPENAQ_DB", PATH_SYNTHETIC: "SYNTHETIC"}

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# Default units for the normalised block (documentation for clients — CO is
# converted to µg/m³ during normalisation when a station reports mg/m³).
UNITS: Dict[str, str] = {p: "µg/m³" for p in POLLUTANTS}


def cache_key(latitude: float, longitude: float) -> str:
    """Canonical Redis key, coordinate-bucketed at 3 dp (~110 m)."""
    return f"{CACHE_PREFIX}:{latitude:.3f}:{longitude:.3f}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def synthetic_air_quality() -> Dict[str, Any]:
    """Deterministic port air — the last-ditch rung, clearly tagged.
    Typical Nhava Sheva shoulder-season values (pm10 92 -> MODERATE)."""
    return {
        "pm25": 48.0, "pm10": 92.0, "no2": 31.0,
        "so2": 14.0, "co": 610.0, "o3": 42.0,
        "air_quality_status": "MODERATE",
        "source": "SYNTHETIC",
        "observed_at": None,
        "stations": [],
        "synthetic": True,
    }


# Module-level cache primitives (monkeypatchable in tests; best-effort like
# services.traffic.service — a Redis outage must never fail the request).
async def _cache_put(key: str, value: Dict[str, Any], ttl: int) -> None:
    wrapped = {"cached_at": _now_iso(), "value": value}
    try:
        await redis_io.cache_set(key, wrapped, ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("air_quality_cache_put_failed", key=key, error=str(exc))


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = await redis_io.cache_get(key)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("air_quality_cache_get_failed", key=key, error=str(exc))
        return None
    if not isinstance(raw, dict) or "value" not in raw:
        return None
    return {"value": raw["value"], "cached_at": raw.get("cached_at"),
            "age_s": _age_seconds(raw.get("cached_at"))}


def _age_seconds(cached_at: Optional[str]) -> Optional[float]:
    if not cached_at:
        return None
    try:
        then = datetime.fromisoformat(cached_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round((datetime.now(tz=timezone.utc) - then).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


class AirQualityService:
    """Fetch and normalise OpenAQ port air quality with the
    LIVE -> CACHED -> DATABASE -> SYNTHETIC fallback chain."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[OpenAQClient] = None,
        repository: Optional[AirQualityRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._client = client or OpenAQClient()
        self._repo = repository or AirQualityRepository(dsn)
        self.cache_ttl_s = cache_ttl_s

    @property
    def configured(self) -> bool:
        """Always True — OpenAQ needs no API key (see OpenAQClient.configured)."""
        return self._client.configured

    # ---------------------------------------------------------------- current
    async def current(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Current port air quality. Never raises for an upstream failure —
        it degrades through CACHED and DATABASE to SYNTHETIC and says so in
        the metadata."""
        key = cache_key(latitude, longitude)

        air_quality: Optional[Dict[str, Any]] = None
        path = PATH_LIVE

        # ------------------------------------------------ LIVE rung (OpenAQ)
        try:
            air_quality = await self._client.fetch_latest(latitude, longitude)
        except OpenAQError as exc:
            log.warning("air_quality_live_failed", lat=latitude, lon=longitude,
                        error=str(exc))

        # ------------------------------------------------ CACHED rung (Redis)
        cache_age_s: Optional[float] = None
        if air_quality is None:
            cached = await _cache_get(key)
            if cached is not None and cached["value"].get("air_quality"):
                air_quality = cached["value"]["air_quality"]
                path = PATH_CACHED
                cache_age_s = cached.get("age_s")

        # -------------------------------------------- DATABASE rung (Postgres)
        if air_quality is None:
            row = await self._db_fallback(latitude, longitude)
            if row is not None:
                air_quality = row["value"]
                path = PATH_DATABASE
                cache_age_s = row.get("age_s")

        # ---------------------------------------------- SYNTHETIC rung (floor)
        if air_quality is None:
            air_quality, path = synthetic_air_quality(), PATH_SYNTHETIC

        # ------------------------------------ write-back + persist (LIVE only)
        if path == PATH_LIVE:
            await _cache_put(key, {"air_quality": air_quality}, self.cache_ttl_s)
            await self._persist(latitude, longitude, air_quality)

        if path == PATH_LIVE:
            status = STATUS_LIVE
        elif path == PATH_SYNTHETIC:
            status = STATUS_OFFLINE
        else:
            status = STATUS_DEGRADED

        return {
            "status": status,
            "source": _PATH_SOURCE[path],
            "decision_path": path,
            "location": {"latitude": latitude, "longitude": longitude},
            "air_quality": air_quality,
            "cache_age_s": cache_age_s,
            "units": UNITS,
            "timestamp": _now_iso(),
        }

    # ---------------------------------------------------------------- history
    async def readings(self, *, latitude: Optional[float] = None,
                       longitude: Optional[float] = None,
                       limit: int = 100, offset: int = 0) -> Tuple[list, int]:
        """Persisted reading history (newest first) + total count."""
        items = await self._repo.list_readings(latitude=latitude, longitude=longitude,
                                               limit=limit, offset=offset)
        total = await self._repo.count_readings(latitude=latitude, longitude=longitude)
        return items, total

    # ---------------------------------------------------------------- helpers
    async def _db_fallback(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        """Rebuild an air_quality block from the last persisted reading (rung 3)."""
        try:
            row = await self._repo.latest_reading(latitude, longitude)
        except Exception as exc:  # noqa: BLE001 - DB down => keep degrading
            log.warning("air_quality_db_fallback_failed", error=str(exc))
            return None
        if row is None:
            return None
        payload = row.get("payload") or {}
        block = payload.get("air_quality") or {
            "pm25": _as_float(row.get("pm25")),
            "pm10": _as_float(row.get("pm10")),
            "no2": _as_float(row.get("no2")),
            "so2": _as_float(row.get("so2")),
            "co": _as_float(row.get("co")),
            "o3": _as_float(row.get("o3")),
            "air_quality_status": row.get("aq_status") or "UNKNOWN",
            "source": row.get("source") or "OPENAQ",
            "observed_at": None,
            "stations": [],
        }
        created_at = row.get("created_at")
        cached_at = created_at.isoformat() if isinstance(created_at, datetime) else None
        return {"value": block, "cached_at": cached_at,
                "age_s": _age_seconds(cached_at)}

    async def _persist(self, latitude: float, longitude: float,
                       air_quality: Dict[str, Any]) -> None:
        """Append the LIVE reading to core.air_quality_readings (best-effort)."""
        try:
            await self._repo.insert_reading(
                latitude=latitude, longitude=longitude,
                pm25=air_quality.get("pm25"), pm10=air_quality.get("pm10"),
                no2=air_quality.get("no2"), so2=air_quality.get("so2"),
                co=air_quality.get("co"), o3=air_quality.get("o3"),
                aq_status=air_quality.get("air_quality_status"),
                source="OPENAQ",
                payload={"air_quality": air_quality},
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("air_quality_persist_failed", lat=latitude, lon=longitude,
                        error=str(exc))


def _as_float(value: Any) -> Optional[float]:
    """DB numerics surface as Decimal — JSON-safe floats out, None-safe."""
    return float(value) if value is not None else None


__all__ = ["AirQualityService", "cache_key", "synthetic_air_quality",
           "STATUS_LIVE", "STATUS_DEGRADED", "STATUS_OFFLINE",
           "PATH_LIVE", "PATH_CACHED", "PATH_DATABASE", "PATH_SYNTHETIC", "UNITS"]
