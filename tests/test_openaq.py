"""OpenAQ Air Quality integration tests (no DB / no network required).

Covers the required scenarios:
  * successful stations + latest response  (client, httpx.MockTransport)
  * empty station response                 (typed OpenAQNoData, not a crash)
  * API failure (5xx)                      (retried with backoff, then typed error)
  * timeout                                (client retries then raises OpenAQTimeout)
  * rate limit / auth 4xx                  (fail fast, no retry)
  * normalization logic                    (aq_status breakpoints, CO mg/m³
                                            conversion, nearest-station-wins merge)
  * cache fallback                         (service: LIVE -> CACHED -> DATABASE ->
                                            SYNTHETIC; OpenAQ down never breaks the API)
  * database persistence                   (LIVE readings appended, best-effort)
plus the additive migration 0108 / gateway/air_quality_ext DDL lock-step and
the router posture (same pattern as tests/test_tomtom.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from integrations.openaq import (
    OpenAQClient,
    OpenAQHTTPError,
    OpenAQInvalidResponse,
    OpenAQNoData,
    OpenAQTimeout,
    aq_status,
    pollutant_status,
)
from services.air_quality import AirQualityService
from services.air_quality import service as aq_service


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
def _locations_payload() -> dict:
    """A representative /v3/locations answer: one station with 6 sensors."""
    return {
        "results": [
            {
                "id": 5599,
                "name": "Nhava Sheva",
                "sensors": [
                    {"id": 1, "parameter": {"name": "pm25", "units": "µg/m³"}},
                    {"id": 2, "parameter": {"name": "pm10", "units": "µg/m³"}},
                    {"id": 3, "parameter": {"name": "no2", "units": "µg/m³"}},
                    {"id": 4, "parameter": {"name": "so2", "units": "µg/m³"}},
                    {"id": 5, "parameter": {"name": "co", "units": "mg/m³"}},
                    {"id": 6, "parameter": {"name": "o3", "units": "µg/m³"}},
                ],
            }
        ],
        "meta": {"found": 1},
    }


def _latest_payload() -> dict:
    """A representative /v3/locations/{id}/latest answer (newest per sensor)."""
    return {
        "results": [
            {"sensorsId": 1, "value": 48.2, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
            {"sensorsId": 2, "value": 92.5, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
            {"sensorsId": 3, "value": 31.0, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
            {"sensorsId": 4, "value": 14.4, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
            {"sensorsId": 5, "value": 0.61, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
            {"sensorsId": 6, "value": 42.0, "datetime": {"utc": "2026-07-29T06:00:00Z"}},
        ]
    }


def _client_with(handler, **kwargs) -> OpenAQClient:
    transport = httpx.MockTransport(handler)
    return OpenAQClient(http_client=httpx.AsyncClient(transport=transport),
                        retries=2, backoff_s=0.0, **kwargs)


def _standard_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v3/locations":
        return httpx.Response(200, json=_locations_payload())
    if request.url.path == "/v3/locations/5599/latest":
        return httpx.Response(200, json=_latest_payload())
    return httpx.Response(404, json={"detail": "not found"})


# ------------------------------------------------------------------ client: happy
def test_client_fetch_latest_success():
    seen = {"locations": 0, "latest": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openaq.org"
        if request.url.path == "/v3/locations":
            seen["locations"] += 1
            assert request.url.params["coordinates"] == "18.95,72.95"
            assert request.url.params["radius"] == "25000"
            return httpx.Response(200, json=_locations_payload())
        seen["latest"] += 1
        assert request.url.path == "/v3/locations/5599/latest"
        return httpx.Response(200, json=_latest_payload())

    norm = _run(_client_with(handler).fetch_latest(18.95, 72.95))
    assert seen == {"locations": 1, "latest": 1}
    assert norm["pm25"] == 48.2
    assert norm["pm10"] == 92.5
    assert norm["no2"] == 31.0
    assert norm["so2"] == 14.4
    assert norm["co"] == 610.0                       # 0.61 mg/m³ -> µg/m³
    assert norm["o3"] == 42.0
    assert norm["air_quality_status"] == "MODERATE"  # pm25 48 / pm10 92
    assert norm["source"] == "OPENAQ"
    assert norm["observed_at"] == "2026-07-29T06:00:00Z"
    assert norm["stations"] == ["Nhava Sheva"]


def test_client_merges_nearest_station_first():
    """Later (farther) stations only fill pollutants the nearest one lacks."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/locations":
            return httpx.Response(200, json={"results": [
                {"id": 1, "name": "Near", "sensors": [
                    {"id": 11, "parameter": {"name": "pm25", "units": "µg/m³"}}]},
                {"id": 2, "name": "Far", "sensors": [
                    {"id": 21, "parameter": {"name": "pm25", "units": "µg/m³"}},
                    {"id": 22, "parameter": {"name": "pm10", "units": "µg/m³"}}]},
            ]})
        if request.url.path == "/v3/locations/1/latest":
            return httpx.Response(200, json={"results": [
                {"sensorsId": 11, "value": 20.0,
                 "datetime": {"utc": "2026-07-29T06:00:00Z"}}]})
        return httpx.Response(200, json={"results": [
            {"sensorsId": 21, "value": 99.0, "datetime": {"utc": "2026-07-29T05:00:00Z"}},
            {"sensorsId": 22, "value": 40.0, "datetime": {"utc": "2026-07-29T05:00:00Z"}}]})

    norm = _run(_client_with(handler).fetch_latest(18.95, 72.95))
    assert norm["pm25"] == 20.0        # nearest station wins
    assert norm["pm10"] == 40.0        # farther station fills the gap
    assert norm["stations"] == ["Near", "Far"]


# --------------------------------------------------------- normalization details
def test_aq_status_breakpoints():
    assert pollutant_status("pm25", 20) == "GOOD"
    assert pollutant_status("pm25", 30) == "GOOD"          # boundary
    assert pollutant_status("pm25", 55) == "MODERATE"
    assert pollutant_status("pm25", 100) == "UNHEALTHY"
    assert pollutant_status("pm25", 200) == "VERY_UNHEALTHY"
    assert pollutant_status("pm10", 120) == "UNHEALTHY"    # doc's dust cue >120
    assert pollutant_status("pm25", None) == "UNKNOWN"
    assert pollutant_status("nox", 50) == "UNKNOWN"         # unknown parameter


def test_aq_status_worst_pollutant_wins():
    assert aq_status({"pm25": 10, "pm10": 30}) == "GOOD"
    assert aq_status({"pm25": 10, "pm10": 130}) == "UNHEALTHY"
    assert aq_status({"pm25": 130, "no2": 20}) == "VERY_UNHEALTHY"
    assert aq_status({"pm25": None, "pm10": None}) == "UNKNOWN"
    assert aq_status({}) == "UNKNOWN"


# ---------------------------------------------------------------- client: failure
def test_client_empty_stations_raises_no_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "meta": {"found": 0}})

    with pytest.raises(OpenAQNoData):
        _run(_client_with(handler).fetch_latest(18.95, 72.95))


def test_client_stations_without_values_raise_no_data():
    """Stations exist but every sensor value is missing -> still NoData."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v3/locations":
            return httpx.Response(200, json=_locations_payload())
        return httpx.Response(200, json={"results": []})

    with pytest.raises(OpenAQNoData):
        _run(_client_with(handler).fetch_latest(18.95, 72.95))


def test_client_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(OpenAQTimeout):
        _run(_client_with(handler).fetch_latest(18.95, 72.95))
    assert calls["n"] == 3  # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"detail": "overloaded"})

    with pytest.raises(OpenAQHTTPError) as exc:
        _run(_client_with(handler).fetch_latest(18.95, 72.95))
    assert exc.value.status_code == 503
    assert calls["n"] == 3


def test_client_rate_limit_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"detail": "rate limit exceeded"})

    with pytest.raises(OpenAQHTTPError) as exc:
        _run(_client_with(handler).fetch_latest(18.95, 72.95))
    assert exc.value.status_code == 429
    assert exc.value.is_rate_limited
    assert calls["n"] == 1  # retrying would burn the rate budget


def test_client_auth_error_fails_fast_and_redacts_key():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.headers["X-API-Key"] == "sekret-key-123"
        return httpx.Response(401, json={"detail": "invalid api key"})

    with pytest.raises(OpenAQHTTPError) as exc:
        _run(_client_with(handler, api_key="sekret-key-123").fetch_latest(18.95, 72.95))
    assert exc.value.status_code == 401
    assert exc.value.is_auth_error
    assert calls["n"] == 1
    assert "sekret-key-123" not in str(exc.value)


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>",
                              headers={"content-type": "text/html"})

    with pytest.raises(OpenAQInvalidResponse):
        _run(_client_with(handler).fetch_latest(18.95, 72.95))


# --------------------------------------------------------------- service fixtures
class _StubOpenAQClient:
    """OpenAQClient stand-in with a success/failure switch."""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    @property
    def configured(self) -> bool:
        return True

    async def fetch_latest(self, lat, lon):
        if not self.ok:
            raise OpenAQTimeout("openaq down")
        return {
            "pm25": 48.2, "pm10": 92.5, "no2": 31.0,
            "so2": 14.4, "co": 610.0, "o3": 42.0,
            "air_quality_status": "MODERATE",
            "source": "OPENAQ",
            "observed_at": "2026-07-29T06:00:00Z",
            "stations": ["Nhava Sheva"],
        }


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
        store[key] = {"value": value, "cached_at": "2026-07-29T06:05:00+00:00"}

    async def get(key):
        raw = store.get(key)
        if raw is None:
            return None
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 60.0}

    monkeypatch.setattr(aq_service, "_cache_put", put)
    monkeypatch.setattr(aq_service, "_cache_get", get)
    return store


def _service(client, repo=None) -> AirQualityService:
    return AirQualityService(client=client, repository=repo or _StubRepo())


# ------------------------------------------------------------------ service: live
def test_service_live_persists_and_caches(fake_cache):
    repo = _StubRepo()
    out = _run(_service(_StubOpenAQClient(), repo).current(18.95, 72.95))

    assert out["status"] == "LIVE"
    assert out["source"] == "OPENAQ"
    assert out["decision_path"] == "LIVE"
    assert out["location"] == {"latitude": 18.95, "longitude": 72.95}
    aq = out["air_quality"]
    assert aq["pm25"] == 48.2
    assert aq["pm10"] == 92.5
    assert aq["no2"] == 31.0
    assert aq["air_quality_status"] == "MODERATE"
    # Database persistence: LIVE reading appended with the pollutant scalars.
    assert len(repo.inserted) == 1
    ins = repo.inserted[0]
    assert ins["source"] == "OPENAQ"
    assert ins["pm25"] == 48.2
    assert ins["aq_status"] == "MODERATE"
    assert ins["payload"]["air_quality"]["co"] == 610.0
    # Cache write-back holds the full block.
    cached = list(fake_cache.values())[0]["value"]
    assert cached["air_quality"]["pm25"] == 48.2


# --------------------------------------------------------- service: cache fallback
def test_service_cache_fallback(fake_cache):
    """OpenAQ LIVE once, then down -> the answer replays from the Redis rung."""
    _run(_service(_StubOpenAQClient()).current(18.95, 72.95))          # primes cache
    out = _run(_service(_StubOpenAQClient(ok=False)).current(18.95, 72.95))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "OPENAQ_CACHE"
    assert out["decision_path"] == "CACHED"
    assert out["air_quality"]["pm25"] == 48.2       # last good answer
    assert out["cache_age_s"] == 60.0


def test_service_db_fallback(fake_cache):
    """Redis empty -> the last persisted core.air_quality_readings row answers."""
    row = {
        "latitude": 18.95, "longitude": 72.95,
        "pm25": 61, "pm10": 130, "no2": 44, "so2": 12, "co": 700, "o3": 38,
        "aq_status": "UNHEALTHY",
        "source": "OPENAQ",
        "payload": {},
        "created_at": None,
    }
    out = _run(_service(_StubOpenAQClient(ok=False), _StubRepo(latest=row))
               .current(18.95, 72.95))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "OPENAQ_DB"
    assert out["decision_path"] == "DATABASE"
    assert out["air_quality"]["pm25"] == 61.0
    assert out["air_quality"]["air_quality_status"] == "UNHEALTHY"


def test_service_synthetic_floor(fake_cache):
    """Nothing live/cached/persisted -> deterministic synthetic, clearly tagged."""
    out = _run(_service(_StubOpenAQClient(ok=False)).current(18.95, 72.95))

    assert out["status"] == "OFFLINE"                    # never a hard failure
    assert out["source"] == "SYNTHETIC"
    assert out["decision_path"] == "SYNTHETIC"
    assert out["air_quality"]["synthetic"] is True       # clearly tagged
    assert out["air_quality"]["air_quality_status"] == "MODERATE"


# ------------------------------------------------------------------ router / config
def test_router_exposes_current_and_health():
    from gateway.routers import air_quality as aqr

    paths = {r.path for r in aqr.router.routes}
    for p in ("/api/air-quality/current", "/api/air-quality/health"):
        assert p in paths, f"missing route {p}"


def test_existing_weather_and_traffic_routes_untouched():
    """Additive only — the pre-existing weather/traffic surfaces must survive."""
    from gateway.routers import traffic as tr
    from gateway.routers import weather as wr

    traffic_paths = {r.path for r in tr.router.routes}
    for p in ("/api/traffic/current", "/api/traffic/health",
              "/api/traffic/predict", "/api/traffic/snapshots"):
        assert p in traffic_paths, f"pre-existing route {p} lost"
    weather_paths = {r.path for r in wr.router.routes}
    for p in ("/api/weather/current", "/api/weather/health"):
        assert p in weather_paths, f"pre-existing route {p} lost"


# -------------------------------------------------------------- DDL lock-step
def test_migration_0108_additive_and_in_lock_step():
    """0108 must only CREATE new objects, and gateway/air_quality_ext must mirror them."""
    from gateway import air_quality_ext

    migration = (Path(__file__).resolve().parents[1]
                 / "infra" / "postgres" / "v3" / "0108_air_quality.sql").read_text()
    ext_ddl = "\n".join(air_quality_ext._DDL)

    for col in ("latitude", "longitude", "pm25", "pm10", "no2", "so2", "co",
                "o3", "aq_status", "source", "payload", "created_at"):
        assert col in migration, f"column {col} missing from migration 0108"
        assert col in ext_ddl, f"column {col} missing from air_quality_ext DDL"
    assert "core.air_quality_readings" in migration
    assert "CREATE TABLE IF NOT EXISTS" in migration
    assert "DROP" not in migration.upper()
    assert "ALTER" not in migration.upper()   # additive only — touches nothing existing
