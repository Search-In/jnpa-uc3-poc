"""WorldTides tide-block integration tests (no DB / no network required).

Covers the required scenarios (same pattern as tests/test_openweather.py):
  * successful heights+extremes response (client, httpx.MockTransport)
  * normalisation: current height, next high/low, RISING/FALLING state
  * stale samples / missing extremes degrade to null — never fabricated
  * timeout                            (client retries then raises)
  * 5xx handling                       (retried with backoff, then typed error)
  * 401 / 429                          (fail fast, no retry — credits budget)
  * API-level error in a 200 body      ({"status": 4xx, "error": ...})
  * invalid response                   (non-JSON / non-object body)
  * key redaction                      (the key rides in the query string and
                                        must never surface in logs/exceptions)
  * service ladder                     (WORLDTIDES LIVE -> OPEN_METEO_MARINE
                                        -> CACHED -> ANALYTIC, per-block, an
                                        outage never breaks /api/weather)
  * backward compatibility             (no key -> status/source aggregation
                                        identical to the pre-tide contract)
plus router wiring (NO new endpoints) and the migration-0110 / weather_ext
DDL lock-step.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from integrations.openmeteo import OpenMeteoTimeout
from integrations.worldtides import (
    WorldTidesClient,
    WorldTidesHTTPError,
    WorldTidesInvalidResponse,
    WorldTidesNotConfigured,
    WorldTidesResponse,
    WorldTidesTimeout,
    WorldTidesUnavailable,
)
from services.weather import WeatherService, service as weather_service

KEY = "TEST-WORLDTIDES-KEY-9876"


def _run(coro):
    return asyncio.run(coro)


def _now() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


# --------------------------------------------------------------- canned payloads
def _wt_payload(now: float | None = None) -> dict:
    """A representative v3 heights+extremes answer centred on now."""
    t = now if now is not None else _now()
    return {
        "status": 200, "callCount": 2,
        "requestLat": 18.9489, "requestLon": 72.9492,
        "responseLat": 18.9167, "responseLon": 72.75,
        "atlas": "TPXO", "station": "Mumbai (Bombay)",
        "heights": [
            {"dt": int(t - 1800), "height": 1.10},
            {"dt": int(t - 60), "height": 1.24},      # nearest to now
            {"dt": int(t + 1740), "height": 1.41},
        ],
        "extremes": [
            {"dt": int(t - 7200), "height": -1.62, "type": "Low"},
            {"dt": int(t + 3 * 3600), "height": 2.31, "type": "High"},
            {"dt": int(t + 9 * 3600), "height": -1.87, "type": "Low"},
        ],
    }


def _client_with(handler, **kwargs) -> WorldTidesClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("api_key", KEY)
    return WorldTidesClient(http_client=httpx.AsyncClient(transport=transport),
                            retries=2, backoff_s=0.0, **kwargs)


# ------------------------------------------------------------------ client: happy
def test_client_fetch_tides_success():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        assert f"key={KEY}" in url and "heights=" in url and "extremes=" in url
        assert "datum=MSL" in url
        return httpx.Response(200, json=_wt_payload())

    parsed = _run(_client_with(handler).fetch_tides(18.9489, 72.9492))
    block = parsed.normalize()
    assert block["tide_height"] == 1.24            # nearest sample to now
    assert block["tide_state"] == "RISING"         # next extreme is a High
    assert block["next_high_tide"]["height"] == 2.31
    assert block["next_low_tide"]["height"] == -1.87
    assert block["station"] == "Mumbai (Bombay)"
    assert block["datum"] == "MSL"
    assert block["synthetic"] is False
    assert "_dt" not in (block["next_high_tide"] or {})   # internal key stripped


def test_normalize_never_fabricates():
    # Samples 6 h old + no future extremes -> every derived field is null.
    stale = _now() - 6 * 3600
    payload = _wt_payload(stale)
    payload["extremes"] = [e for e in payload["extremes"] if e["dt"] < _now()]
    block = WorldTidesResponse.model_validate(payload).normalize()
    assert block["tide_height"] is None
    assert block["next_high_tide"] is None
    assert block["next_low_tide"] is None
    assert block["tide_state"] is None


def test_normalize_falling_state():
    t = _now()
    payload = _wt_payload(t)
    # Nearest future extreme is a Low -> FALLING.
    payload["extremes"] = [{"dt": int(t + 3600), "height": -1.5, "type": "Low"},
                           {"dt": int(t + 7 * 3600), "height": 2.2, "type": "High"}]
    block = WorldTidesResponse.model_validate(payload).normalize()
    assert block["tide_state"] == "FALLING"


# ---------------------------------------------------------------- client: failures
def test_client_not_configured_raises():
    with pytest.raises(WorldTidesNotConfigured):
        _run(WorldTidesClient(api_key="").fetch_tides(18.9, 72.9))


def test_client_timeout_retries_then_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow")

    with pytest.raises(WorldTidesTimeout):
        _run(_client_with(handler).fetch_tides(18.9, 72.9))
    assert len(calls) == 3                        # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"status": 503, "error": "maintenance"})

    with pytest.raises(WorldTidesHTTPError) as exc:
        _run(_client_with(handler).fetch_tides(18.9, 72.9))
    assert exc.value.status_code == 503
    assert len(calls) == 3


@pytest.mark.parametrize("status,flag", [(401, "is_auth_error"), (429, "is_rate_limited")])
def test_client_4xx_fails_fast_no_retry(status, flag):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, json={"status": status, "error": "rejected"})

    with pytest.raises(WorldTidesHTTPError) as exc:
        _run(_client_with(handler).fetch_tides(18.9, 72.9))
    assert getattr(exc.value, flag) is True
    assert len(calls) == 1                        # fail fast — credits budget


def test_client_api_level_error_in_200_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 400, "error": "invalid parameters"})

    with pytest.raises(WorldTidesHTTPError) as exc:
        _run(_client_with(handler).fetch_tides(18.9, 72.9))
    assert exc.value.status_code == 400


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(WorldTidesInvalidResponse):
        _run(_client_with(handler).fetch_tides(18.9, 72.9))


def test_client_key_never_in_exception_or_redacted_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"boom https://www.worldtides.info/api/v3?key={KEY}&lat=18.9")

    with pytest.raises(WorldTidesUnavailable) as exc:
        _run(_client_with(handler).fetch_tides(18.9, 72.9))
    assert KEY not in str(exc.value)
    assert "***" in str(exc.value)
    client = WorldTidesClient(api_key=KEY)
    assert KEY not in client._redact(f"url?key={KEY}")
    assert WorldTidesClient(api_key="")._redact("plain") == "plain"


# --------------------------------------------------------------- service fixtures
class _StubOM:
    """OpenMeteoClient stand-in (same shape as tests/test_weather.py)."""

    def __init__(self, weather_ok: bool = True, marine_ok: bool = True) -> None:
        self.weather_ok = weather_ok
        self.marine_ok = marine_ok
        from integrations.openmeteo.schemas import MarineResponse, WeatherResponse
        self._weather = WeatherResponse.model_validate({
            "latitude": 18.95, "longitude": 72.95,
            "current": {"time": "2026-07-30T10:00", "temperature_2m": 30.1,
                        "wind_speed_10m": 15.4, "wind_direction_10m": 240,
                        "wind_gusts_10m": 28.8, "precipitation": 0.2,
                        "weather_code": 80},
            "hourly": {"time": ["2026-07-30T10:00"], "temperature_2m": [30.1],
                       "wind_speed_10m": [15.4], "wind_direction_10m": [240],
                       "wind_gusts_10m": [28.8], "precipitation": [0.2],
                       "weather_code": [80], "visibility": [8000.0]},
        })
        self._marine = MarineResponse.model_validate({
            "latitude": 18.95, "longitude": 72.95,
            "current": {"time": "2026-07-30T10:00", "wave_height": 1.2,
                        "wave_period": 5.0, "swell_wave_height": 0.9,
                        "sea_level_height_msl": 0.6},
            "hourly": {"time": ["2026-07-30T10:00"], "wave_height": [1.2],
                       "wave_period": [5.0], "swell_wave_height": [0.9],
                       "sea_level_height_msl": [0.6]},
        })

    async def fetch_weather(self, lat, lon, *, forecast_hours=24):
        if not self.weather_ok:
            raise OpenMeteoTimeout("weather down")
        return self._weather

    async def fetch_marine(self, lat, lon):
        if not self.marine_ok:
            raise OpenMeteoTimeout("marine down")
        return self._marine


class _StubWT:
    """WorldTidesClient stand-in with a success/failure switch."""

    def __init__(self, ok: bool = True, configured: bool = True) -> None:
        self.ok = ok
        self._configured = configured

    @property
    def configured(self) -> bool:
        return self._configured

    async def fetch_tides(self, lat, lon):
        if not self.ok:
            raise WorldTidesTimeout("worldtides down")
        return WorldTidesResponse.model_validate(_wt_payload())


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
        store[key] = {"value": value, "cached_at": "2026-07-30T09:55:00+00:00"}

    async def get(key):
        raw = store.get(key)
        if raw is None:
            return None
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 300.0}

    monkeypatch.setattr(weather_service, "_cache_put", put)
    monkeypatch.setattr(weather_service, "_cache_get", get)
    return store


def _service(om=None, wt=None, repo=None) -> WeatherService:
    from integrations.openweather import OpenWeatherClient

    return WeatherService(
        client=om or _StubOM(),
        openweather_client=OpenWeatherClient(api_key=""),
        worldtides_client=wt if wt is not None else _StubWT(),
        repository=repo or _StubRepo(),
    )


# ------------------------------------------------------------ service: the ladder
def test_service_tide_live_caches_and_persists(fake_cache):
    repo = _StubRepo()
    out = _run(_service(repo=repo).current(18.9489, 72.9492))
    assert out["status"] == "LIVE"
    assert out["sources"]["tide"] == "LIVE"
    assert out["tide"]["tide_source"] == "WORLDTIDES"
    assert out["tide"]["tide_height"] == 1.24
    assert out["tide"]["fetched_at"]
    # Cached combined value carries the LIVE tide block for the CACHED rung.
    cached = next(iter(fake_cache.values()))["value"]
    assert cached["tide"]["tide_source"] == "WORLDTIDES"
    # Persisted with the new columns + full block in payload.
    assert repo.inserted[0]["tide_height"] == 1.24
    assert repo.inserted[0]["tide_state"] == "RISING"
    assert repo.inserted[0]["payload"]["tide"]["station"] == "Mumbai (Bombay)"


def test_service_tide_falls_to_marine_when_worldtides_down(fake_cache):
    out = _run(_service(wt=_StubWT(ok=False)).current(18.9489, 72.9492))
    assert out["sources"]["tide"] == "OPEN_METEO_MARINE"
    tide = out["tide"]
    assert tide["tide_source"] == "OPEN_METEO_MARINE"
    assert tide["tide_height"] == 0.6              # live marine sea level (MSL)
    assert tide["next_high_tide"] is None          # never fabricated
    assert tide["next_low_tide"] is None
    assert tide["tide_state"] is None
    # A configured-but-failing provider degrades the answer.
    assert out["status"] == "DEGRADED"


def test_service_tide_cached_rung(fake_cache):
    svc = _service(repo=_StubRepo())
    _run(svc.current(18.9489, 72.9492))            # warm the cache LIVE
    # Now WorldTides AND marine die -> tide must come from the cache.
    out = _run(_service(om=_StubOM(marine_ok=False), wt=_StubWT(ok=False))
               .current(18.9489, 72.9492))
    assert out["sources"]["tide"] == "CACHED"
    assert out["tide"]["tide_source"] == "WORLDTIDES_CACHE"
    assert out["tide"]["tide_height"] == 1.24


def test_service_tide_analytic_floor_never_breaks(fake_cache):
    out = _run(_service(om=_StubOM(weather_ok=False, marine_ok=False),
                        wt=_StubWT(ok=False)).current(18.9489, 72.9492))
    assert out["status"] == "OFFLINE"
    tide = out["tide"]
    assert out["sources"]["tide"] == "ANALYTIC"
    assert tide["tide_source"] == "ANALYTIC"
    assert tide["synthetic"] is True
    assert -1.7 <= tide["tide_height"] <= 1.7
    assert tide["next_high_tide"]["time"] and tide["next_low_tide"]["time"]
    assert tide["tide_state"] in ("RISING", "FALLING")


def test_service_backward_compatible_without_key(fake_cache):
    """No WORLDTIDES_API_KEY -> the pre-tide status/source contract is
    unchanged; the tide block is served from keyless rungs as extra info."""
    out = _run(_service(wt=_StubWT(configured=False)).current(18.9489, 72.9492))
    assert out["status"] == "LIVE"                 # NOT degraded by the tide rung
    assert out["source"] == "OPEN_METEO"
    assert out["decision_path"] == "LIVE"
    assert out["sources"]["tide"] == "OPEN_METEO_MARINE"
    assert out["tide"]["tide_height"] == 0.6
    # The legacy blocks are untouched.
    for legacy in ("weather", "marine", "openweather", "units", "timestamp"):
        assert legacy in out


def test_analytic_tide_is_deterministic():
    from services.weather.service import analytic_tide
    a = analytic_tide(now_epoch=1_785_400_000.0)
    b = analytic_tide(now_epoch=1_785_400_000.0)
    assert a == b
    assert a["synthetic"] is True and a["datum"] == "MSL"


# --------------------------------------------------------------- router + DDL
def test_no_new_routes_added():
    from gateway.routers import weather as weather_router
    paths = sorted({r.path for r in weather_router.router.routes})
    assert paths == ["/api/weather/current", "/api/weather/health",
                     "/api/weather/readings"]


def test_migration_0110_and_ext_ddl_in_lock_step():
    """gateway/weather_ext.py must mirror infra/postgres/v3/0110_weather_tide.sql."""
    from gateway import weather_ext
    repo_root = Path(__file__).resolve().parents[1]
    migration = (repo_root / "infra" / "postgres" / "v3"
                 / "0110_weather_tide.sql").read_text()
    ext_ddl = "\n".join(weather_ext._DDL)
    for col in ("tide_height", "tide_state"):
        assert col in migration, f"column {col} missing from migration 0110"
        assert col in ext_ddl, f"column {col} missing from weather_ext DDL"
