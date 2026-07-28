"""Weather service — combine Open-Meteo Weather + Marine with graceful fallback.

The single entry point the /api/weather router calls. Thin over
:class:`integrations.openmeteo.OpenMeteoClient` (HTTP) and
:class:`services.weather.repository.WeatherRepository` (persistence), in the
same mould as services.shipping_lines.service: stateless apart from config.

Fallback chain (per data block, mirroring gateway/fallback.py's vocabulary —
an Open-Meteo outage must NEVER break the API):

    LIVE       -> fresh fetch from Open-Meteo (weather + marine concurrently)
    CACHED     -> last good combined answer from Redis
                  (key jnpa:cache:weather:{lat}:{lon}), else the last persisted
                  core.weather_reading row for the coordinate
    SYNTHETIC  -> deterministic port-area conditions, clearly tagged

Response contract (source metadata always attached):
    status  LIVE      — both blocks fresh
            DEGRADED  — at least one block served from a fallback rung
            OFFLINE   — nothing real available, both blocks synthetic
    source  OPEN_METEO | OPEN_METEO_CACHE | SYNTHETIC   (worst rung that fired)

A fully-LIVE answer is written back to Redis + core.weather_reading
(best-effort — an infra blip never fails the request being served).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from jnpa_shared import redis_io
from jnpa_shared.logging import get_logger

from integrations.openmeteo import OpenMeteoClient, OpenMeteoError

from .repository import WeatherRepository

log = get_logger("services.weather.service")

# Cache-key convention matches gateway/cache.py: jnpa:cache:{api}:{key}.
CACHE_PREFIX = "jnpa:cache:weather"
DEFAULT_CACHE_TTL_S = 600

# Decision-path rungs (per block) and the source label each one implies.
PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_SYNTHETIC = "SYNTHETIC"
_PATH_RANK = {PATH_LIVE: 0, PATH_CACHED: 1, PATH_SYNTHETIC: 2}
_PATH_SOURCE = {PATH_LIVE: "OPEN_METEO", PATH_CACHED: "OPEN_METEO_CACHE",
                PATH_SYNTHETIC: "SYNTHETIC"}

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# Open-Meteo default units for the normalised blocks (documentation for clients).
UNITS: Dict[str, str] = {
    "temperature": "°C", "wind_speed": "km/h", "wind_direction": "°",
    "wind_gusts": "km/h", "visibility": "m", "precipitation": "mm",
    "wave_height": "m", "wave_period": "s", "swell_wave_height": "m",
    "sea_level_height": "m",
}


def cache_key(latitude: float, longitude: float) -> str:
    """Canonical Redis key, coordinate-bucketed at 3 dp (~110 m)."""
    return f"{CACHE_PREFIX}:{latitude:.3f}:{longitude:.3f}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def synthetic_weather() -> Dict[str, Any]:
    """Deterministic port-area weather — the last-ditch rung, clearly tagged."""
    return {
        "temperature": 29.0, "wind_speed": 18.0, "wind_direction": 250.0,
        "wind_gusts": 30.0, "visibility": 8000.0, "precipitation": 0.0,
        "weather_code": 2, "condition": "Partly cloudy", "observed_at": None,
        "synthetic": True,
    }


def synthetic_marine() -> Dict[str, Any]:
    """Deterministic port-area sea state — the last-ditch rung, clearly tagged."""
    return {
        "wave_height": 1.2, "wave_period": 6.0, "swell_wave_height": 0.9,
        "sea_level_height": 0.6, "observed_at": None, "synthetic": True,
    }


# Module-level cache primitives (monkeypatchable in tests; best-effort like
# gateway/cache.py — a Redis outage must never fail the request being served).
async def _cache_put(key: str, value: Dict[str, Any], ttl: int) -> None:
    wrapped = {"cached_at": _now_iso(), "value": value}
    try:
        await redis_io.cache_set(key, wrapped, ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("weather_cache_put_failed", key=key, error=str(exc))


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = await redis_io.cache_get(key)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("weather_cache_get_failed", key=key, error=str(exc))
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


class WeatherService:
    """Fetch, combine and normalise Open-Meteo weather + marine conditions."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[OpenMeteoClient] = None,
        repository: Optional[WeatherRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._client = client or OpenMeteoClient()
        self._repo = repository or WeatherRepository(dsn)
        self.cache_ttl_s = cache_ttl_s

    # ---------------------------------------------------------------- current
    async def current(self, latitude: float, longitude: float,
                      *, forecast_hours: int = 0) -> Dict[str, Any]:
        """Combined current conditions. Never raises for an upstream failure —
        it degrades through CACHED to SYNTHETIC and says so in the metadata."""
        key = cache_key(latitude, longitude)

        weather, marine, forecast = None, None, []
        weather_path = marine_path = PATH_LIVE

        wres, mres = await asyncio.gather(
            self._client.fetch_weather(latitude, longitude,
                                       forecast_hours=max(forecast_hours, 1)),
            self._client.fetch_marine(latitude, longitude),
            return_exceptions=True,
        )
        if isinstance(wres, OpenMeteoError):
            log.warning("weather_live_failed", lat=latitude, lon=longitude, error=str(wres))
        elif isinstance(wres, BaseException):
            raise wres
        else:
            weather = wres.normalize()
            forecast = wres.forecast(forecast_hours)
        if isinstance(mres, OpenMeteoError):
            log.warning("marine_live_failed", lat=latitude, lon=longitude, error=str(mres))
        elif isinstance(mres, BaseException):
            raise mres
        else:
            marine = mres.normalize()

        # ---------------------------------------------- CACHED rung (Redis → DB)
        cached: Optional[Dict[str, Any]] = None
        cache_age_s: Optional[float] = None
        if weather is None or marine is None:
            cached = await _cache_get(key)
            if cached is None:
                cached = await self._db_cache(latitude, longitude)
            if cached is not None:
                if weather is None and cached["value"].get("weather"):
                    weather, weather_path = cached["value"]["weather"], PATH_CACHED
                if marine is None and cached["value"].get("marine"):
                    marine, marine_path = cached["value"]["marine"], PATH_CACHED
                cache_age_s = cached.get("age_s")

        # ---------------------------------------------- SYNTHETIC rung (floor)
        if weather is None:
            weather, weather_path = synthetic_weather(), PATH_SYNTHETIC
        if marine is None:
            marine, marine_path = synthetic_marine(), PATH_SYNTHETIC

        # ------------------------------------ write-back + persist (LIVE only)
        if weather_path == PATH_LIVE and marine_path == PATH_LIVE:
            await _cache_put(key, {"weather": weather, "marine": marine}, self.cache_ttl_s)
            await self._persist(latitude, longitude, weather, marine)

        worst = max(weather_path, marine_path, key=_PATH_RANK.__getitem__)
        if worst == PATH_LIVE:
            status = STATUS_LIVE
        elif weather_path == marine_path == PATH_SYNTHETIC:
            status = STATUS_OFFLINE
        else:
            status = STATUS_DEGRADED

        response: Dict[str, Any] = {
            "status": status,
            "source": _PATH_SOURCE[worst],
            "decision_path": worst,
            "location": {"latitude": latitude, "longitude": longitude},
            "weather": weather,
            "marine": marine,
            "sources": {"weather": weather_path, "marine": marine_path},
            "cache_age_s": cache_age_s,
            "units": UNITS,
            "timestamp": _now_iso(),
        }
        if forecast_hours > 0:
            response["forecast"] = forecast
        return response

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
    async def _db_cache(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        """Rebuild a cache-shaped value from the last persisted reading (rung 2b)."""
        try:
            row = await self._repo.latest_reading(latitude, longitude)
        except Exception as exc:  # noqa: BLE001 - DB down => keep degrading
            log.warning("weather_db_fallback_failed", error=str(exc))
            return None
        if row is None:
            return None
        payload = row.get("payload") or {}
        weather = payload.get("weather") or {
            "temperature": _as_float(row.get("temperature")),
            "wind_speed": _as_float(row.get("wind_speed")),
            "wind_direction": _as_float(row.get("wind_direction")),
            "wind_gusts": None,
            "visibility": _as_float(row.get("visibility")),
            "precipitation": _as_float(row.get("precipitation")),
            "weather_code": None, "condition": None, "observed_at": None,
        }
        marine = payload.get("marine") or {
            "wave_height": _as_float(row.get("wave_height")),
            "wave_period": _as_float(row.get("wave_period")),
            "swell_wave_height": None, "sea_level_height": None, "observed_at": None,
        }
        created_at = row.get("created_at")
        cached_at = created_at.isoformat() if isinstance(created_at, datetime) else None
        return {"value": {"weather": weather, "marine": marine},
                "cached_at": cached_at, "age_s": _age_seconds(cached_at)}

    async def _persist(self, latitude: float, longitude: float,
                       weather: Dict[str, Any], marine: Dict[str, Any]) -> None:
        """Append the LIVE reading to core.weather_reading (best-effort)."""
        try:
            await self._repo.insert_reading(
                latitude=latitude, longitude=longitude,
                temperature=weather.get("temperature"),
                wind_speed=weather.get("wind_speed"),
                wind_direction=weather.get("wind_direction"),
                visibility=weather.get("visibility"),
                precipitation=weather.get("precipitation"),
                wave_height=marine.get("wave_height"),
                wave_period=marine.get("wave_period"),
                source="OPEN_METEO",
                payload={"weather": weather, "marine": marine},
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("weather_persist_failed", lat=latitude, lon=longitude, error=str(exc))


def _as_float(value: Any) -> Optional[float]:
    """DB numerics surface as Decimal — JSON-safe floats out, None-safe."""
    return float(value) if value is not None else None


__all__ = ["WeatherService", "cache_key", "synthetic_weather", "synthetic_marine",
           "STATUS_LIVE", "STATUS_DEGRADED", "STATUS_OFFLINE",
           "PATH_LIVE", "PATH_CACHED", "PATH_SYNTHETIC", "UNITS"]
