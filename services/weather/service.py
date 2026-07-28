"""Weather service — combine Open-Meteo Weather + Marine + OpenWeatherMap with
graceful fallback.

The single entry point the /api/weather router calls. Thin over
:class:`integrations.openmeteo.OpenMeteoClient` +
:class:`integrations.openweather.OpenWeatherClient` (HTTP) and
:class:`services.weather.repository.WeatherRepository` (persistence), in the
same mould as services.shipping_lines.service: stateless apart from config.

Fallback chain (per data block, mirroring gateway/fallback.py's vocabulary —
a provider outage must NEVER break the API):

    LIVE       -> fresh fetch (weather + marine + openweather concurrently)
    CACHED     -> last good combined answer from Redis
                  (key jnpa:cache:weather:{lat}:{lon}), else the last persisted
                  core.weather_reading row for the coordinate
    SYNTHETIC  -> deterministic port-area conditions, clearly tagged

OpenWeatherMap is ADDITIVE: it only participates when OPENWEATHER_API_KEY is
configured. Unconfigured -> the ``openweather`` block is ``null`` and its rung
is DISABLED (excluded from status/source aggregation), so the pre-existing
Open-Meteo-only contract is unchanged. If OpenWeather fails while Open-Meteo
answers, only the ``openweather`` block degrades — Open-Meteo data still
returns and the API never fails because OpenWeather is down.

Response contract (source metadata always attached):
    status  LIVE      — every active block fresh
            DEGRADED  — at least one active block served from a fallback rung
            OFFLINE   — nothing real available, all active blocks synthetic
    source  OPEN_METEO+OPENWEATHER (both live) | OPEN_METEO |
            OPEN_METEO_CACHE | SYNTHETIC   (worst rung that fired)

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
from integrations.openweather import OpenWeatherClient, OpenWeatherError

from .repository import WeatherRepository

log = get_logger("services.weather.service")

# Cache-key convention matches gateway/cache.py: jnpa:cache:{api}:{key}.
CACHE_PREFIX = "jnpa:cache:weather"
DEFAULT_CACHE_TTL_S = 600

# Decision-path rungs (per block) and the source label each one implies.
PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_SYNTHETIC = "SYNTHETIC"
# DISABLED = provider not configured (OpenWeather without an API key). The
# block is null and the rung is excluded from status/source aggregation.
PATH_DISABLED = "DISABLED"
_PATH_RANK = {PATH_LIVE: 0, PATH_CACHED: 1, PATH_SYNTHETIC: 2}
_PATH_SOURCE = {PATH_LIVE: "OPEN_METEO", PATH_CACHED: "OPEN_METEO_CACHE",
                PATH_SYNTHETIC: "SYNTHETIC"}

# Source label when every active block (incl. OpenWeather) is fresh.
SOURCE_COMBINED = "OPEN_METEO+OPENWEATHER"

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# |Open-Meteo temp − OpenWeather temp| beyond this flags the cross-provider
# temperature validation (openweather.temperature_consistent = False).
TEMP_VALIDATION_TOLERANCE_C = 3.0

# Default units for the normalised blocks (documentation for clients).
UNITS: Dict[str, str] = {
    "temperature": "°C", "wind_speed": "km/h", "wind_direction": "°",
    "wind_gusts": "km/h", "visibility": "m", "precipitation": "mm",
    "wave_height": "m", "wave_period": "s", "swell_wave_height": "m",
    "sea_level_height": "m",
    # openweather block
    "humidity": "%", "clouds": "%", "rain": "mm", "pressure": "hPa",
    "feels_like": "°C", "temperature_delta": "°C",
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


def synthetic_openweather() -> Dict[str, Any]:
    """Deterministic port-area OpenWeather block — the last-ditch rung, clearly
    tagged. Values agree with :func:`synthetic_weather` so cross-provider
    temperature validation stays green on the synthetic floor."""
    return {
        "temperature": 29.0, "feels_like": 32.0, "humidity": 75.0,
        "pressure": 1005.0, "rain": 0.0, "clouds": 40.0,
        "condition": "Cloudy", "condition_id": 802, "description": "scattered clouds",
        "label": "CLOUDY", "wind_speed": 18.0, "wind_direction": 250.0,
        "visibility": 8000.0, "station": None, "observed_at": None,
        "synthetic": True,
    }


def _validate_temperature(openweather: Optional[Dict[str, Any]],
                          weather: Optional[Dict[str, Any]]) -> None:
    """Cross-provider temperature validation, annotated onto the openweather
    block: delta vs Open-Meteo and whether it sits inside the tolerance band."""
    if not openweather:
        return
    ow_t = openweather.get("temperature")
    om_t = (weather or {}).get("temperature")
    if ow_t is None or om_t is None:
        openweather["temperature_delta"] = None
        openweather["temperature_consistent"] = None
        return
    delta = round(float(ow_t) - float(om_t), 1)
    openweather["temperature_delta"] = delta
    openweather["temperature_consistent"] = abs(delta) <= TEMP_VALIDATION_TOLERANCE_C


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
    """Fetch, combine and normalise Open-Meteo weather + marine conditions,
    enriched with OpenWeatherMap when an API key is configured."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[OpenMeteoClient] = None,
        openweather_client: Optional[OpenWeatherClient] = None,
        repository: Optional[WeatherRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._client = client or OpenMeteoClient()
        self._ow_client = openweather_client or OpenWeatherClient()
        self._repo = repository or WeatherRepository(dsn)
        self.cache_ttl_s = cache_ttl_s

    @property
    def openweather_enabled(self) -> bool:
        """True when the OpenWeather provider participates (API key present)."""
        return self._ow_client.configured

    # ---------------------------------------------------------------- current
    async def current(self, latitude: float, longitude: float,
                      *, forecast_hours: int = 0) -> Dict[str, Any]:
        """Combined current conditions. Never raises for an upstream failure —
        it degrades through CACHED to SYNTHETIC and says so in the metadata."""
        key = cache_key(latitude, longitude)
        ow_enabled = self.openweather_enabled

        weather, marine, openweather, forecast = None, None, None, []
        weather_path = marine_path = PATH_LIVE
        openweather_path = PATH_LIVE if ow_enabled else PATH_DISABLED

        tasks = [
            self._client.fetch_weather(latitude, longitude,
                                       forecast_hours=max(forecast_hours, 1)),
            self._client.fetch_marine(latitude, longitude),
        ]
        if ow_enabled:
            tasks.append(self._ow_client.fetch_current(latitude, longitude))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        wres, mres = results[0], results[1]
        owres = results[2] if ow_enabled else None

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
        if ow_enabled:
            if isinstance(owres, OpenWeatherError):
                log.warning("openweather_live_failed", lat=latitude, lon=longitude,
                            error=str(owres))
            elif isinstance(owres, BaseException):
                raise owres
            else:
                openweather = owres.normalize()

        # ---------------------------------------------- CACHED rung (Redis → DB)
        cached: Optional[Dict[str, Any]] = None
        cache_age_s: Optional[float] = None
        ow_missing = ow_enabled and openweather is None
        if weather is None or marine is None or ow_missing:
            cached = await _cache_get(key)
            if cached is None:
                cached = await self._db_cache(latitude, longitude)
            if cached is not None:
                if weather is None and cached["value"].get("weather"):
                    weather, weather_path = cached["value"]["weather"], PATH_CACHED
                if marine is None and cached["value"].get("marine"):
                    marine, marine_path = cached["value"]["marine"], PATH_CACHED
                if ow_missing and cached["value"].get("openweather"):
                    openweather = cached["value"]["openweather"]
                    openweather_path = PATH_CACHED
                cache_age_s = cached.get("age_s")

        # ---------------------------------------------- SYNTHETIC rung (floor)
        if weather is None:
            weather, weather_path = synthetic_weather(), PATH_SYNTHETIC
        if marine is None:
            marine, marine_path = synthetic_marine(), PATH_SYNTHETIC
        if ow_enabled and openweather is None:
            openweather, openweather_path = synthetic_openweather(), PATH_SYNTHETIC

        # Cross-provider temperature validation (annotated on the OW block).
        _validate_temperature(openweather, weather)

        # ------------------------------------ write-back + persist (LIVE only)
        # Open-Meteo blocks LIVE => cache/persist; the openweather block rides
        # along only when it is LIVE too (a stale OW block is never written back).
        if weather_path == PATH_LIVE and marine_path == PATH_LIVE:
            value: Dict[str, Any] = {"weather": weather, "marine": marine}
            if openweather_path == PATH_LIVE and openweather is not None:
                value["openweather"] = openweather
            await _cache_put(key, value, self.cache_ttl_s)
            await self._persist(
                latitude, longitude, weather, marine,
                openweather=value.get("openweather"))

        active_paths = [weather_path, marine_path]
        if openweather_path != PATH_DISABLED:
            active_paths.append(openweather_path)
        worst = max(active_paths, key=_PATH_RANK.__getitem__)
        if worst == PATH_LIVE:
            status = STATUS_LIVE
        elif all(p == PATH_SYNTHETIC for p in active_paths):
            status = STATUS_OFFLINE
        else:
            status = STATUS_DEGRADED

        if worst == PATH_LIVE and openweather_path == PATH_LIVE:
            source = SOURCE_COMBINED
        else:
            source = _PATH_SOURCE[worst]

        response: Dict[str, Any] = {
            "status": status,
            "source": source,
            "decision_path": worst,
            "location": {"latitude": latitude, "longitude": longitude},
            "weather": weather,
            "marine": marine,
            "openweather": openweather,
            "sources": {"weather": weather_path, "marine": marine_path,
                        "openweather": openweather_path},
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
        value: Dict[str, Any] = {"weather": weather, "marine": marine}
        # openweather is only present in payloads persisted while OW was LIVE.
        if payload.get("openweather"):
            value["openweather"] = payload["openweather"]
        return {"value": value, "cached_at": cached_at, "age_s": _age_seconds(cached_at)}

    async def _persist(self, latitude: float, longitude: float,
                       weather: Dict[str, Any], marine: Dict[str, Any],
                       openweather: Optional[Dict[str, Any]] = None) -> None:
        """Append the LIVE reading to core.weather_reading (best-effort)."""
        payload: Dict[str, Any] = {"weather": weather, "marine": marine}
        if openweather is not None:
            payload["openweather"] = openweather
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
                humidity=(openweather or {}).get("humidity"),
                clouds=(openweather or {}).get("clouds"),
                source=SOURCE_COMBINED if openweather is not None else "OPEN_METEO",
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("weather_persist_failed", lat=latitude, lon=longitude, error=str(exc))


def _as_float(value: Any) -> Optional[float]:
    """DB numerics surface as Decimal — JSON-safe floats out, None-safe."""
    return float(value) if value is not None else None


__all__ = ["WeatherService", "cache_key", "synthetic_weather", "synthetic_marine",
           "synthetic_openweather",
           "STATUS_LIVE", "STATUS_DEGRADED", "STATUS_OFFLINE",
           "PATH_LIVE", "PATH_CACHED", "PATH_SYNTHETIC", "PATH_DISABLED",
           "SOURCE_COMBINED", "TEMP_VALIDATION_TOLERANCE_C", "UNITS"]
