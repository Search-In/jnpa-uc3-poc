"""Open-Meteo Weather + Marine integration tests (no DB / no network required).

Covers the required scenarios:
  * successful weather API response  (client, httpx.MockTransport)
  * successful marine API response   (client, httpx.MockTransport)
  * API timeout handling             (client retries then raises OpenMeteoTimeout)
  * external API failure             (5xx retried; 4xx fail-fast; invalid body)
  * cache fallback behaviour         (service: LIVE -> CACHED -> SYNTHETIC)
plus router wiring and the migration/ext DDL lock-step (same pattern as
tests/test_shipping_lines_schema.py).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from integrations.openmeteo import (
    OpenMeteoClient,
    OpenMeteoHTTPError,
    OpenMeteoInvalidResponse,
    OpenMeteoTimeout,
)
from services.weather import WeatherService, service as weather_service


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
def _weather_payload() -> dict:
    return {
        "latitude": 18.95,
        "longitude": 72.95,
        "current": {
            "time": "2026-07-27T10:00",
            "temperature_2m": 30.1,
            "wind_speed_10m": 15.4,
            "wind_direction_10m": 240,
            "wind_gusts_10m": 28.8,
            "precipitation": 0.2,
            "weather_code": 80,
        },
        "hourly": {
            "time": ["2026-07-27T09:00", "2026-07-27T10:00", "2026-07-27T11:00"],
            "temperature_2m": [29.5, 30.1, 30.4],
            "wind_speed_10m": [14.0, 15.4, 16.1],
            "wind_direction_10m": [235, 240, 245],
            "wind_gusts_10m": [26.0, 28.8, 29.5],
            "precipitation": [0.0, 0.2, 0.4],
            "weather_code": [3, 80, 80],
            "visibility": [9000.0, 8000.0, 7500.0],
        },
    }


def _marine_payload() -> dict:
    return {
        "latitude": 18.95,
        "longitude": 72.95,
        "current": {
            "time": "2026-07-27T10:00",
            "wave_height": 1.2,
            "wave_period": 5.0,
            "swell_wave_height": 0.9,
            "sea_level_height_msl": 0.6,
        },
        "hourly": {
            "time": ["2026-07-27T10:00"],
            "wave_height": [1.2],
            "wave_period": [5.0],
            "swell_wave_height": [0.9],
            "sea_level_height_msl": [0.6],
        },
    }


def _client_with(handler) -> OpenMeteoClient:
    transport = httpx.MockTransport(handler)
    return OpenMeteoClient(http_client=httpx.AsyncClient(transport=transport),
                           retries=2, backoff_s=0.0)


# ------------------------------------------------------------------ client: happy
def test_client_fetch_weather_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.open-meteo.com"
        assert request.url.params["latitude"] == "18.9489"
        assert "visibility" in request.url.params["hourly"]
        return httpx.Response(200, json=_weather_payload())

    resp = _run(_client_with(handler).fetch_weather(18.9489, 72.9492))
    norm = resp.normalize()
    assert norm["temperature"] == 30.1
    assert norm["wind_speed"] == 15.4
    assert norm["wind_direction"] == 240
    assert norm["wind_gusts"] == 28.8
    assert norm["precipitation"] == 0.2
    # Visibility is hourly-only — must resolve at the current hour (10:00).
    assert norm["visibility"] == 8000.0
    assert norm["weather_code"] == 80
    assert norm["condition"] == "Slight rain showers"


def test_client_fetch_marine_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "marine-api.open-meteo.com"
        assert "wave_height" in request.url.params["current"]
        return httpx.Response(200, json=_marine_payload())

    norm = _run(_client_with(handler).fetch_marine(18.9489, 72.9492)).normalize()
    assert norm["wave_height"] == 1.2
    assert norm["wave_period"] == 5.0
    assert norm["swell_wave_height"] == 0.9
    assert norm["sea_level_height"] == 0.6


def test_client_weather_forecast_slice():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_weather_payload())

    resp = _run(_client_with(handler).fetch_weather(18.9489, 72.9492, forecast_hours=2))
    fc = resp.forecast(2)
    assert [f["time"] for f in fc] == ["2026-07-27T10:00", "2026-07-27T11:00"]
    assert fc[0]["condition"] == "Slight rain showers"


# ---------------------------------------------------------------- client: failure
def test_client_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(OpenMeteoTimeout):
        _run(_client_with(handler).fetch_weather(18.9, 72.9))
    assert calls["n"] == 3  # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": True, "reason": "overloaded"})

    with pytest.raises(OpenMeteoHTTPError) as exc:
        _run(_client_with(handler).fetch_marine(18.9, 72.9))
    assert exc.value.status_code == 503
    assert exc.value.reason == "overloaded"
    assert calls["n"] == 3


def test_client_4xx_fails_fast_no_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})

    with pytest.raises(OpenMeteoHTTPError) as exc:
        _run(_client_with(handler).fetch_weather(999.0, 72.9))
    assert exc.value.status_code == 400
    assert calls["n"] == 1  # a rejected request is never retried


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})  # no coords/current/hourly

    with pytest.raises(OpenMeteoInvalidResponse):
        _run(_client_with(handler).fetch_weather(18.9, 72.9))


# --------------------------------------------------------------- service fixtures
class _StubClient:
    """OpenMeteoClient stand-in with per-endpoint success/failure switches."""

    def __init__(self, weather_ok: bool = True, marine_ok: bool = True) -> None:
        self.weather_ok = weather_ok
        self.marine_ok = marine_ok
        from integrations.openmeteo.schemas import MarineResponse, WeatherResponse
        self._weather = WeatherResponse.model_validate(_weather_payload())
        self._marine = MarineResponse.model_validate(_marine_payload())

    async def fetch_weather(self, lat, lon, *, forecast_hours=24):
        if not self.weather_ok:
            raise OpenMeteoTimeout("weather down")
        return self._weather

    async def fetch_marine(self, lat, lon):
        if not self.marine_ok:
            raise OpenMeteoTimeout("marine down")
        return self._marine


class _StubRepo:
    """WeatherRepository stand-in — records inserts, serves one canned row."""

    def __init__(self, latest=None) -> None:
        self.latest = latest
        self.inserted: list[dict] = []

    async def insert_reading(self, **kwargs):
        self.inserted.append(kwargs)
        return len(self.inserted)

    async def latest_reading(self, lat, lon):
        return self.latest

    async def list_readings(self, **kwargs):
        return []

    async def count_readings(self, **kwargs):
        return 0


@pytest.fixture()
def fake_cache(monkeypatch):
    """In-memory stand-in for the module-level Redis cache primitives."""
    store: dict[str, dict] = {}

    async def put(key, value, ttl):
        store[key] = {"value": value, "cached_at": "2026-07-27T09:55:00+00:00"}

    async def get(key):
        raw = store.get(key)
        if raw is None:
            return None
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 300.0}

    monkeypatch.setattr(weather_service, "_cache_put", put)
    monkeypatch.setattr(weather_service, "_cache_get", get)
    return store


def _service(client, repo=None, **kwargs) -> WeatherService:
    # OpenWeather explicitly disabled (no API key) — these tests pin the
    # original Open-Meteo-only contract regardless of the ambient environment.
    from integrations.openweather import OpenWeatherClient

    kwargs.setdefault("openweather_client", OpenWeatherClient(api_key=""))
    return WeatherService(client=client, repository=repo or _StubRepo(), **kwargs)


# ------------------------------------------------------------------ service: LIVE
def test_service_live_combines_caches_and_persists(fake_cache):
    repo = _StubRepo()
    out = _run(_service(_StubClient(), repo).current(18.9489, 72.9492))

    assert out["status"] == "LIVE"
    assert out["source"] == "OPEN_METEO"
    assert out["decision_path"] == "LIVE"
    assert out["location"] == {"latitude": 18.9489, "longitude": 72.9492}
    assert out["weather"]["temperature"] == 30.1
    assert out["weather"]["visibility"] == 8000.0
    assert out["marine"]["wave_height"] == 1.2
    assert out["marine"]["wave_period"] == 5.0
    assert out["timestamp"]
    # A fully-LIVE answer is written back to the cache AND persisted.
    assert len(fake_cache) == 1
    assert len(repo.inserted) == 1
    assert repo.inserted[0]["source"] == "OPEN_METEO"
    assert repo.inserted[0]["wave_height"] == 1.2


def test_service_forecast_included_when_requested(fake_cache):
    out = _run(_service(_StubClient()).current(18.9489, 72.9492, forecast_hours=2))
    assert len(out["forecast"]) == 2
    out2 = _run(_service(_StubClient()).current(18.9489, 72.9492))
    assert "forecast" not in out2


# ---------------------------------------------------------------- service: CACHED
def test_service_cache_fallback_degraded(fake_cache):
    svc = _service(_StubClient())
    _run(svc.current(18.9489, 72.9492))                       # primes the cache
    down = _service(_StubClient(weather_ok=False, marine_ok=False))
    out = _run(down.current(18.9489, 72.9492))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "OPEN_METEO_CACHE"
    assert out["decision_path"] == "CACHED"
    assert out["cache_age_s"] == 300.0
    assert out["weather"]["temperature"] == 30.1              # last good answer replayed
    assert out["marine"]["wave_height"] == 1.2


def test_service_partial_failure_is_degraded(fake_cache):
    """Weather live, marine down, nothing cached -> marine degrades to SYNTHETIC."""
    out = _run(_service(_StubClient(marine_ok=False)).current(18.9489, 72.9492))
    assert out["status"] == "DEGRADED"
    assert out["sources"] == {"weather": "LIVE", "marine": "SYNTHETIC",
                              "openweather": "DISABLED"}
    assert out["weather"]["temperature"] == 30.1
    assert out["marine"]["synthetic"] is True


def test_service_db_fallback_when_redis_empty(fake_cache):
    """Redis empty but a persisted reading exists -> CACHED from the DB rung."""
    row = {
        "latitude": 18.9489, "longitude": 72.9492,
        "payload": {"weather": {"temperature": 28.5, "visibility": 9000.0},
                    "marine": {"wave_height": 1.0, "wave_period": 6.0}},
        "created_at": None,
    }
    down = _service(_StubClient(weather_ok=False, marine_ok=False), _StubRepo(latest=row))
    out = _run(down.current(18.9489, 72.9492))
    assert out["status"] == "DEGRADED"
    assert out["decision_path"] == "CACHED"
    assert out["weather"]["temperature"] == 28.5
    assert out["marine"]["wave_height"] == 1.0


# ------------------------------------------------------------- service: SYNTHETIC
def test_service_synthetic_floor_is_offline(fake_cache):
    down = _service(_StubClient(weather_ok=False, marine_ok=False))
    out = _run(down.current(18.9489, 72.9492))

    assert out["status"] == "OFFLINE"
    assert out["source"] == "SYNTHETIC"
    assert out["decision_path"] == "SYNTHETIC"
    assert out["weather"]["synthetic"] is True
    assert out["marine"]["synthetic"] is True
    # The API still answers with the full contract — it NEVER breaks.
    assert out["weather"]["temperature"] is not None
    assert out["marine"]["wave_height"] is not None


# ------------------------------------------------------------------ router wiring
def test_expected_routes_present():
    from gateway.routers import weather as wr

    paths = {r.path for r in wr.router.routes}
    for p in ("/api/weather/current", "/api/weather/readings", "/api/weather/health"):
        assert p in paths, f"missing route {p}"


def test_default_coords_come_from_settings():
    """JNPA coordinates are configurable (jnpa_shared settings), never hardcoded."""
    from gateway.routers.weather import _default_coords
    from jnpa_shared.config import get_settings

    s = get_settings()
    assert _default_coords(None, None) == (s.port_lat, s.port_lon)
    assert _default_coords(19.0, 72.9) == (19.0, 72.9)


# -------------------------------------------------------------- DDL lock-step
def test_migration_and_ext_ddl_in_lock_step():
    """gateway/weather_ext.py must mirror infra/postgres/v3/0105_weather_reading.sql."""
    from gateway import weather_ext

    migration = (Path(__file__).resolve().parents[1]
                 / "infra" / "postgres" / "v3" / "0105_weather_reading.sql").read_text()
    ext_ddl = "\n".join(weather_ext._DDL)

    for col in ("latitude", "longitude", "temperature", "wind_speed", "wind_direction",
                "visibility", "precipitation", "wave_height", "wave_period",
                "source", "payload", "created_at"):
        assert col in migration, f"column {col} missing from migration 0105"
        assert col in ext_ddl, f"column {col} missing from weather_ext DDL"
    assert "core.weather_reading" in migration
    assert "core.weather_reading" in ext_ddl
    assert "DROP" not in migration.upper()
    assert "ALTER TABLE" not in migration.upper()
