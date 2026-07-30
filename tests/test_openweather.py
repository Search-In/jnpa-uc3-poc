"""OpenWeatherMap integration tests (no DB / no network required).

Covers the required scenarios:
  * valid OpenWeather response       (client, httpx.MockTransport)
  * invalid API key (401)            (fail fast, no retry)
  * rate limit (429)                 (fail fast, no retry)
  * timeout                          (client retries then raises OpenWeatherTimeout)
  * OpenWeather unavailable          (5xx retried; network errors; invalid body)
  * Open-Meteo fallback works        (service: OW down/degraded never breaks the API;
                                      unconfigured -> original Open-Meteo contract)
plus the additive migration 0106 / weather_ext DDL lock-step (same pattern as
tests/test_weather.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from integrations.openmeteo.schemas import MarineResponse, WeatherResponse
from integrations.openweather import (
    OpenWeatherClient,
    OpenWeatherHTTPError,
    OpenWeatherInvalidResponse,
    OpenWeatherNotConfigured,
    OpenWeatherResponse,
    OpenWeatherTimeout,
    condition_label,
)
from services.weather import WeatherService, service as weather_service
from services.weather.service import SOURCE_COMBINED


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
def _ow_payload() -> dict:
    """A representative /data/2.5/weather answer (units=metric)."""
    return {
        "coord": {"lon": 72.95, "lat": 18.95},
        "weather": [{"id": 802, "main": "Clouds", "description": "scattered clouds",
                     "icon": "03d"}],
        "base": "stations",
        "main": {"temp": 30.4, "feels_like": 35.1, "temp_min": 30.4, "temp_max": 30.4,
                 "pressure": 1004, "humidity": 70},
        "visibility": 6000,
        "wind": {"speed": 5.0, "deg": 250, "gust": 9.2},
        "clouds": {"all": 40},
        "dt": 1753693200,
        "sys": {"country": "IN"},
        "timezone": 19800,
        "id": 1279064,
        "name": "Uran",
        "cod": 200,
    }


def _meteo_weather_payload() -> dict:
    return {
        "latitude": 18.95,
        "longitude": 72.95,
        "current": {
            "time": "2026-07-28T10:00",
            "temperature_2m": 29.8,
            "wind_speed_10m": 15.4,
            "wind_direction_10m": 240,
            "wind_gusts_10m": 28.8,
            "precipitation": 0.2,
            "weather_code": 3,
        },
    }


def _meteo_marine_payload() -> dict:
    return {
        "latitude": 18.95,
        "longitude": 72.95,
        "current": {
            "time": "2026-07-28T10:00",
            "wave_height": 1.2,
            "wave_period": 5.0,
            "swell_wave_height": 0.9,
            "sea_level_height_msl": 0.6,
        },
    }


def _client_with(handler, **kwargs) -> OpenWeatherClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("api_key", "test-key")
    return OpenWeatherClient(http_client=httpx.AsyncClient(transport=transport),
                             retries=2, backoff_s=0.0, **kwargs)


# ------------------------------------------------------------------ client: happy
def test_client_fetch_current_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openweathermap.org"
        assert request.url.path == "/data/2.5/weather"
        assert request.url.params["lat"] == "18.9489"
        assert request.url.params["lon"] == "72.9492"
        assert request.url.params["appid"] == "test-key"
        assert request.url.params["units"] == "metric"
        return httpx.Response(200, json=_ow_payload())

    norm = _run(_client_with(handler).fetch_current(18.9489, 72.9492)).normalize()
    assert norm["temperature"] == 30.4
    assert norm["humidity"] == 70
    assert norm["rain"] == 0.0            # no rain block => 0 mm, not null
    assert norm["clouds"] == 40
    assert norm["condition"] == "Cloudy"
    assert norm["condition_id"] == 802
    assert norm["label"] == "CLOUDY"
    assert norm["wind_speed"] == 18.0     # 5.0 m/s -> km/h
    assert norm["visibility"] == 6000
    assert norm["observed_at"].startswith("2025") or norm["observed_at"].startswith("2026")


def test_client_rain_block_parsed():
    payload = _ow_payload()
    payload["weather"] = [{"id": 501, "main": "Rain", "description": "moderate rain"}]
    payload["rain"] = {"1h": 2.4}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    norm = _run(_client_with(handler).fetch_current(18.9, 72.9)).normalize()
    assert norm["rain"] == 2.4
    assert norm["condition"] == "Rain"
    assert norm["label"] == "RAIN"


def test_condition_labels():
    assert condition_label(800) == "CLEAR"
    assert condition_label(803) == "CLOUDY"
    assert condition_label(211) == "STORM"
    assert condition_label(502) == "RAIN"
    assert condition_label(741) == "LOW_VISIBILITY"
    assert condition_label(None) is None


# ---------------------------------------------------------------- client: failure
def test_client_not_configured_raises():
    with pytest.raises(OpenWeatherNotConfigured):
        _run(OpenWeatherClient(api_key="").fetch_current(18.9, 72.9))


def test_client_invalid_api_key_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"cod": 401, "message": "Invalid API key."})

    with pytest.raises(OpenWeatherHTTPError) as exc:
        _run(_client_with(handler, api_key="bad-key").fetch_current(18.9, 72.9))
    assert exc.value.status_code == 401
    assert exc.value.is_auth_error
    assert exc.value.reason == "Invalid API key."
    assert calls["n"] == 1  # a rejected key is never retried


def test_client_rate_limit_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"cod": 429, "message": "Rate limit exceeded"})

    with pytest.raises(OpenWeatherHTTPError) as exc:
        _run(_client_with(handler).fetch_current(18.9, 72.9))
    assert exc.value.status_code == 429
    assert exc.value.is_rate_limited
    assert calls["n"] == 1  # retrying would burn the call budget


def test_client_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(OpenWeatherTimeout):
        _run(_client_with(handler).fetch_current(18.9, 72.9))
    assert calls["n"] == 3  # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"cod": 503, "message": "overloaded"})

    with pytest.raises(OpenWeatherHTTPError) as exc:
        _run(_client_with(handler).fetch_current(18.9, 72.9))
    assert exc.value.status_code == 503
    assert calls["n"] == 3


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})  # no main/weather

    with pytest.raises(OpenWeatherInvalidResponse):
        _run(_client_with(handler).fetch_current(18.9, 72.9))


# --------------------------------------------------------------- service fixtures
class _StubMeteoClient:
    """OpenMeteoClient stand-in — always succeeds (Open-Meteo is not under test)."""

    def __init__(self) -> None:
        self._weather = WeatherResponse.model_validate(_meteo_weather_payload())
        self._marine = MarineResponse.model_validate(_meteo_marine_payload())

    async def fetch_weather(self, lat, lon, *, forecast_hours=24):
        return self._weather

    async def fetch_marine(self, lat, lon):
        return self._marine


class _StubOWClient:
    """OpenWeatherClient stand-in with success/failure/configured switches."""

    def __init__(self, ok: bool = True, configured: bool = True) -> None:
        self.ok = ok
        self._configured = configured
        self._resp = OpenWeatherResponse.model_validate(_ow_payload())

    @property
    def configured(self) -> bool:
        return self._configured

    async def fetch_current(self, lat, lon):
        if not self.ok:
            raise OpenWeatherTimeout("openweather down")
        return self._resp


class _StubRepo:
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
    store: dict[str, dict] = {}

    async def put(key, value, ttl):
        store[key] = {"value": value, "cached_at": "2026-07-28T09:55:00+00:00"}

    async def get(key):
        raw = store.get(key)
        if raw is None:
            return None
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 300.0}

    monkeypatch.setattr(weather_service, "_cache_put", put)
    monkeypatch.setattr(weather_service, "_cache_get", get)
    return store


def _service(ow_client, repo=None) -> WeatherService:
    return WeatherService(client=_StubMeteoClient(),
                          openweather_client=ow_client,
                          repository=repo or _StubRepo())


# --------------------------------------------------------------- service: combined
def test_service_combined_live(fake_cache):
    repo = _StubRepo()
    out = _run(_service(_StubOWClient(), repo).current(18.9489, 72.9492))

    assert out["status"] == "LIVE"
    assert out["source"] == SOURCE_COMBINED == "OPEN_METEO+OPENWEATHER"
    assert out["sources"] == {"weather": "LIVE", "marine": "LIVE",
                              "openweather": "LIVE", "tide": "OPEN_METEO_MARINE"}
    # Open-Meteo blocks untouched by the enrichment.
    assert out["weather"]["temperature"] == 29.8
    assert out["marine"]["wave_height"] == 1.2
    # OpenWeather block per the required contract.
    ow = out["openweather"]
    assert ow["temperature"] == 30.4
    assert ow["humidity"] == 70
    assert ow["rain"] == 0.0
    assert ow["clouds"] == 40
    assert ow["condition"] == "Cloudy"
    # Cross-provider temperature validation: |30.4 - 29.8| within tolerance.
    assert ow["temperature_delta"] == 0.6
    assert ow["temperature_consistent"] is True
    # Persisted with the OW scalars + combined source; payload carries the block.
    assert len(repo.inserted) == 1
    ins = repo.inserted[0]
    assert ins["source"] == SOURCE_COMBINED
    assert ins["humidity"] == 70
    assert ins["clouds"] == 40
    assert ins["payload"]["openweather"]["condition"] == "Cloudy"
    # Cache write-back includes the openweather block.
    assert list(fake_cache.values())[0]["value"]["openweather"]["humidity"] == 70


def test_service_temperature_validation_flags_disagreement(fake_cache):
    ow_client = _StubOWClient()
    ow_client._resp = OpenWeatherResponse.model_validate(
        {**_ow_payload(), "main": {"temp": 38.0, "humidity": 70}})
    out = _run(_service(ow_client).current(18.9489, 72.9492))
    assert out["openweather"]["temperature_delta"] == 8.2
    assert out["openweather"]["temperature_consistent"] is False


# ------------------------------------------------------- service: OW never breaks API
def test_service_openweather_down_meteo_still_returns(fake_cache):
    """Open-Meteo fallback works: an OpenWeather outage degrades ONLY its block."""
    out = _run(_service(_StubOWClient(ok=False)).current(18.9489, 72.9492))

    assert out["status"] == "DEGRADED"                    # never a hard failure
    assert out["sources"]["weather"] == "LIVE"            # Open-Meteo still LIVE
    assert out["sources"]["marine"] == "LIVE"
    assert out["sources"]["openweather"] == "SYNTHETIC"   # nothing cached yet
    assert out["weather"]["temperature"] == 29.8          # real Open-Meteo data
    assert out["openweather"]["synthetic"] is True        # clearly tagged


def test_service_openweather_cached_rung(fake_cache):
    """OW LIVE once, then down -> its block replays from the combined cache."""
    _run(_service(_StubOWClient()).current(18.9489, 72.9492))       # primes cache
    out = _run(_service(_StubOWClient(ok=False)).current(18.9489, 72.9492))

    assert out["status"] == "DEGRADED"
    assert out["sources"]["openweather"] == "CACHED"
    assert out["openweather"]["humidity"] == 70           # last good OW answer
    assert out["sources"]["weather"] == "LIVE"


def test_service_unconfigured_keeps_original_contract(fake_cache):
    """No API key -> openweather null/DISABLED and the surface is Open-Meteo-only."""
    repo = _StubRepo()
    out = _run(_service(_StubOWClient(configured=False), repo).current(18.9489, 72.9492))

    assert out["status"] == "LIVE"
    assert out["source"] == "OPEN_METEO"                  # pre-existing label
    assert out["openweather"] is None
    assert out["sources"]["openweather"] == "DISABLED"
    assert repo.inserted[0]["source"] == "OPEN_METEO"
    assert repo.inserted[0]["humidity"] is None


def test_service_db_fallback_replays_openweather(fake_cache):
    """The persisted payload restores the OW block on the DB rung too."""
    row = {
        "latitude": 18.9489, "longitude": 72.9492,
        "payload": {"weather": {"temperature": 28.5},
                    "marine": {"wave_height": 1.0},
                    "openweather": {"temperature": 29.1, "humidity": 64,
                                    "rain": 0.0, "clouds": 25, "condition": "Clear"}},
        "created_at": None,
    }

    class _DownMeteo(_StubMeteoClient):
        async def fetch_weather(self, lat, lon, *, forecast_hours=24):
            from integrations.openmeteo import OpenMeteoTimeout
            raise OpenMeteoTimeout("down")

        async def fetch_marine(self, lat, lon):
            from integrations.openmeteo import OpenMeteoTimeout
            raise OpenMeteoTimeout("down")

    svc = WeatherService(client=_DownMeteo(),
                         openweather_client=_StubOWClient(ok=False),
                         repository=_StubRepo(latest=row))
    out = _run(svc.current(18.9489, 72.9492))
    assert out["status"] == "DEGRADED"
    assert out["sources"] == {"weather": "CACHED", "marine": "CACHED",
                              "openweather": "CACHED", "tide": "ANALYTIC"}
    assert out["openweather"]["humidity"] == 64


# ------------------------------------------------------------------ router / config
def test_router_health_reports_openweather_posture():
    from gateway.routers import weather as wr

    paths = {r.path for r in wr.router.routes}
    for p in ("/api/weather/current", "/api/weather/readings", "/api/weather/health"):
        assert p in paths, f"missing route {p}"


def test_gateway_config_reads_key(monkeypatch):
    monkeypatch.setenv("OPENWEATHER_API_KEY", "abc123")
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_env()
    assert cfg.openweather_api_key == "abc123"
    assert cfg.openweather_enabled is True
    monkeypatch.setenv("OPENWEATHER_API_KEY", "")
    assert GatewayConfig.from_env().openweather_enabled is False


# -------------------------------------------------------------- DDL lock-step
def test_migration_0106_additive_and_in_lock_step():
    """0106 must only ADD columns, and gateway/weather_ext must mirror them."""
    from gateway import weather_ext

    migration = (Path(__file__).resolve().parents[1]
                 / "infra" / "postgres" / "v3" / "0106_weather_openweather.sql").read_text()
    ext_ddl = "\n".join(weather_ext._DDL)

    for col in ("humidity", "clouds"):
        assert col in migration, f"column {col} missing from migration 0106"
        assert col in ext_ddl, f"column {col} missing from weather_ext DDL"
    assert "core.weather_reading" in migration
    assert "ADD COLUMN IF NOT EXISTS" in migration
    assert "DROP" not in migration.upper()
    assert "CREATE TABLE" not in migration.upper()   # additive only — no new table
