"""TomTom Traffic integration tests (no DB / no network required).

Covers the required scenarios:
  * successful flow + incidents response  (client, httpx.MockTransport)
  * invalid API key (401) / forbidden (403)  (fail fast, no retry)
  * rate limit (429)                      (fail fast, no retry)
  * timeout                               (client retries then raises TomTomTimeout)
  * 5xx handling                          (retried with backoff, then typed error)
  * normalization logic                   (congestion_level thresholds, delay,
                                           incident type/severity mapping)
  * cache fallback                        (service: LIVE -> CACHED -> DATABASE ->
                                           SYNTHETIC; TomTom down never breaks the API)
plus the additive migration 0107 / gateway/traffic_ext DDL lock-step and the
router/config posture (same pattern as tests/test_openweather.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from integrations.tomtom import (
    TomTomClient,
    TomTomHTTPError,
    TomTomInvalidResponse,
    TomTomNotConfigured,
    TomTomTimeout,
    congestion_level,
    incident_severity,
    incident_type,
)
from services.traffic import TrafficService
from services.traffic import service as traffic_service


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
def _flow_payload() -> dict:
    """A representative flowSegmentData answer (unit=KMPH)."""
    return {
        "flowSegmentData": {
            "frc": "FRC0",
            "currentSpeed": 43,
            "freeFlowSpeed": 50,
            "currentTravelTime": 540,
            "freeFlowTravelTime": 465,
            "confidence": 0.94,
            "roadClosure": False,
            "coordinates": {"coordinate": [{"latitude": 18.95, "longitude": 72.95}]},
        }
    }


def _incidents_payload() -> dict:
    """A representative incidentDetails (v5) answer."""
    return {
        "incidents": [
            {
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": [[72.95, 18.95]]},
                "properties": {
                    "iconCategory": 9,
                    "magnitudeOfDelay": 2,
                    "events": [{"description": "Roadworks", "code": 701,
                                "iconCategory": 9}],
                    "from": "Y Junction",
                    "to": "Karal Phata",
                    "roadNumbers": ["NH-348"],
                    "delay": 120,
                },
            }
        ]
    }


def _client_with(handler, **kwargs) -> TomTomClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("api_key", "test-key")
    return TomTomClient(http_client=httpx.AsyncClient(transport=transport),
                        retries=2, backoff_s=0.0, **kwargs)


# ------------------------------------------------------------------ client: happy
def test_client_fetch_flow_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tomtom.com"
        assert request.url.path == "/traffic/services/4/flowSegmentData/absolute/10/json"
        assert request.url.params["point"] == "18.9489,72.9492"
        assert request.url.params["unit"] == "KMPH"
        assert request.url.params["key"] == "test-key"
        return httpx.Response(200, json=_flow_payload())

    norm = _run(_client_with(handler).fetch_flow(18.9489, 72.9492)).normalize()
    assert norm["current_speed"] == 43
    assert norm["free_flow_speed"] == 50
    assert norm["current_travel_time"] == 540
    assert norm["free_flow_travel_time"] == 465
    assert norm["congestion_level"] == "LOW"        # 43/50 = 0.86 >= 0.80
    assert norm["delay_seconds"] == 75.0            # 540 - 465
    assert norm["road_closure"] is False
    assert norm["confidence"] == 0.94


def test_client_fetch_incidents_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/traffic/services/5/incidentDetails"
        assert request.url.params["key"] == "test-key"
        assert "bbox" in request.url.params
        return httpx.Response(200, json=_incidents_payload())

    out = _run(_client_with(handler).fetch_incidents(72.9, 18.7, 73.1, 19.0)).normalize()
    assert len(out) == 1
    inc = out[0]
    assert inc["type"] == "ROAD_WORKS"
    assert inc["severity"] == "MODERATE"
    assert inc["description"] == "Roadworks"
    assert inc["road"] == "NH-348 (Y Junction → Karal Phata)"
    assert inc["delay"] == 120


def test_client_fetch_route_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/routing/1/calculateRoute/")
        return httpx.Response(200, json={"routes": [{"summary": {
            "lengthInMeters": 21000, "travelTimeInSeconds": 1500,
            "trafficDelayInSeconds": 180}}]})

    out = _run(_client_with(handler).fetch_route(18.95, 72.95, 18.78, 73.08))
    assert out["length_m"] == 21000
    assert out["travel_time_s"] == 1500
    assert out["traffic_delay_s"] == 180


# --------------------------------------------------------- normalization details
def test_congestion_levels_from_speed_ratio():
    assert congestion_level(48, 50) == "LOW"        # 0.96
    assert congestion_level(40, 50) == "LOW"        # 0.80 boundary
    assert congestion_level(35, 50) == "MEDIUM"     # 0.70
    assert congestion_level(25, 50) == "HIGH"       # 0.50
    assert congestion_level(15, 50) == "SEVERE"     # 0.30
    assert congestion_level(30, 50, road_closure=True) == "SEVERE"
    assert congestion_level(None, 50) == "UNKNOWN"
    assert congestion_level(30, 0) == "UNKNOWN"


def test_incident_type_and_severity_maps():
    assert incident_type(1) == "ACCIDENT"
    assert incident_type(6) == "JAM"
    assert incident_type(8) == "ROAD_CLOSED"
    assert incident_type(14) == "BROKEN_DOWN_VEHICLE"
    assert incident_type(None) == "UNKNOWN"
    assert incident_severity(1) == "MINOR"
    assert incident_severity(3) == "MAJOR"
    assert incident_severity(4) == "CLOSURE"
    assert incident_severity(None) == "UNKNOWN"


def test_flow_delay_never_negative():
    payload = _flow_payload()
    payload["flowSegmentData"]["currentTravelTime"] = 400   # faster than free flow

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    norm = _run(_client_with(handler).fetch_flow(18.9, 72.9)).normalize()
    assert norm["delay_seconds"] == 0.0


# ---------------------------------------------------------------- client: failure
def test_client_not_configured_raises():
    with pytest.raises(TomTomNotConfigured):
        _run(TomTomClient(api_key="").fetch_flow(18.9, 72.9))
    with pytest.raises(TomTomNotConfigured):
        _run(TomTomClient(api_key="").fetch_incidents(72.9, 18.7, 73.1, 19.0))


def test_client_invalid_api_key_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": {"description": "Invalid API key."}})

    with pytest.raises(TomTomHTTPError) as exc:
        _run(_client_with(handler, api_key="bad-key").fetch_flow(18.9, 72.9))
    assert exc.value.status_code == 401
    assert exc.value.is_auth_error
    assert exc.value.reason == "Invalid API key."
    assert calls["n"] == 1  # a rejected key is never retried


def test_client_forbidden_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"error": {"description": "Forbidden"}})

    with pytest.raises(TomTomHTTPError) as exc:
        _run(_client_with(handler).fetch_flow(18.9, 72.9))
    assert exc.value.status_code == 403
    assert exc.value.is_auth_error
    assert calls["n"] == 1


def test_client_rate_limit_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": {"description": "Rate limit exceeded"}})

    with pytest.raises(TomTomHTTPError) as exc:
        _run(_client_with(handler).fetch_flow(18.9, 72.9))
    assert exc.value.status_code == 429
    assert exc.value.is_rate_limited
    assert calls["n"] == 1  # retrying would burn the daily call budget


def test_client_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(TomTomTimeout):
        _run(_client_with(handler).fetch_flow(18.9, 72.9))
    assert calls["n"] == 3  # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"description": "overloaded"}})

    with pytest.raises(TomTomHTTPError) as exc:
        _run(_client_with(handler).fetch_flow(18.9, 72.9))
    assert exc.value.status_code == 503
    assert calls["n"] == 3


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})  # no flowSegmentData

    with pytest.raises(TomTomInvalidResponse):
        _run(_client_with(handler).fetch_flow(18.9, 72.9))


def test_client_key_never_in_exception_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(Exception) as exc:
        _run(_client_with(handler, api_key="sekret-key-123").fetch_flow(18.9, 72.9))
    assert "sekret-key-123" not in str(exc.value)


# --------------------------------------------------------------- service fixtures
class _StubTomTomClient:
    """TomTomClient stand-in with success/failure/configured switches."""

    def __init__(self, ok: bool = True, configured: bool = True) -> None:
        self.ok = ok
        self._configured = configured
        from integrations.tomtom.schemas import FlowSegmentResponse, IncidentsResponse
        self._flow = FlowSegmentResponse.model_validate(_flow_payload())
        self._incidents = IncidentsResponse.model_validate(_incidents_payload())

    @property
    def configured(self) -> bool:
        return self._configured

    async def fetch_flow(self, lat, lon):
        if not self.ok:
            raise TomTomTimeout("tomtom down")
        return self._flow

    async def fetch_incidents(self, *bbox):
        if not self.ok:
            raise TomTomTimeout("tomtom down")
        return self._incidents


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
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 60.0}

    monkeypatch.setattr(traffic_service, "_cache_put", put)
    monkeypatch.setattr(traffic_service, "_cache_get", get)
    return store


def _service(client, repo=None) -> TrafficService:
    return TrafficService(client=client, repository=repo or _StubRepo())


# ------------------------------------------------------------------ service: live
def test_service_live(fake_cache):
    repo = _StubRepo()
    out = _run(_service(_StubTomTomClient(), repo).current(18.9489, 72.9492))

    assert out["status"] == "LIVE"
    assert out["source"] == "TOMTOM"
    assert out["decision_path"] == "LIVE"
    assert out["sources"] == {"traffic": "LIVE", "incidents": "LIVE"}
    assert out["traffic"]["current_speed"] == 43
    assert out["traffic"]["free_flow_speed"] == 50
    assert out["traffic"]["congestion_level"] == "LOW"
    assert out["traffic"]["delay_seconds"] == 75.0
    assert out["incident_count"] == 1
    assert out["incidents"][0]["type"] == "ROAD_WORKS"
    # Persisted with the flow scalars; payload carries the full blocks.
    assert len(repo.inserted) == 1
    ins = repo.inserted[0]
    assert ins["source"] == "TOMTOM"
    assert ins["current_speed"] == 43
    assert ins["congestion_level"] == "LOW"
    assert ins["payload"]["incidents"][0]["severity"] == "MODERATE"
    # Cache write-back includes both blocks.
    cached = list(fake_cache.values())[0]["value"]
    assert cached["traffic"]["current_speed"] == 43
    assert len(cached["incidents"]) == 1


# --------------------------------------------------------- service: cache fallback
def test_service_cache_fallback(fake_cache):
    """TomTom LIVE once, then down -> the answer replays from the Redis rung."""
    _run(_service(_StubTomTomClient()).current(18.9489, 72.9492))       # primes cache
    out = _run(_service(_StubTomTomClient(ok=False)).current(18.9489, 72.9492))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "TOMTOM_CACHE"
    assert out["decision_path"] == "CACHED"
    assert out["sources"] == {"traffic": "CACHED", "incidents": "CACHED"}
    assert out["traffic"]["current_speed"] == 43        # last good answer
    assert out["cache_age_s"] == 60.0


def test_service_db_fallback(fake_cache):
    """Redis empty -> the last persisted core.traffic_reading row answers."""
    row = {
        "latitude": 18.9489, "longitude": 72.9492,
        "current_speed": 38, "free_flow_speed": 50,
        "congestion_level": "MEDIUM", "delay_seconds": 130,
        "payload": {"traffic": {"current_speed": 38, "free_flow_speed": 50,
                                "congestion_level": "MEDIUM", "delay_seconds": 130},
                    "incidents": []},
        "created_at": None,
    }
    out = _run(_service(_StubTomTomClient(ok=False), _StubRepo(latest=row))
               .current(18.9489, 72.9492))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "TOMTOM_DB"
    assert out["decision_path"] == "DATABASE"
    assert out["traffic"]["current_speed"] == 38
    assert out["incidents"] == []


def test_service_synthetic_floor(fake_cache):
    """Nothing live/cached/persisted -> deterministic synthetic, clearly tagged."""
    out = _run(_service(_StubTomTomClient(ok=False)).current(18.9489, 72.9492))

    assert out["status"] == "OFFLINE"                    # never a hard failure
    assert out["source"] == "SYNTHETIC"
    assert out["decision_path"] == "SYNTHETIC"
    assert out["traffic"]["synthetic"] is True           # clearly tagged
    assert out["traffic"]["congestion_level"] == "LOW"
    assert out["incidents"] == []                        # no fabricated incidents


def test_service_unconfigured_never_breaks(fake_cache):
    """No API key -> the LIVE rung is skipped, the surface still answers."""
    out = _run(_service(_StubTomTomClient(configured=False)).current(18.9489, 72.9492))

    assert out["status"] == "OFFLINE"
    assert out["source"] == "SYNTHETIC"
    assert out["traffic"]["synthetic"] is True


# ------------------------------------------------------------------ router / config
def test_router_exposes_current_and_health():
    from gateway.routers import traffic as tr

    paths = {r.path for r in tr.router.routes}
    for p in ("/api/traffic/current", "/api/traffic/health"):
        assert p in paths, f"missing route {p}"
    # Pre-existing congestion-model endpoints untouched (additive only).
    for p in ("/api/traffic/predict", "/api/traffic/snapshots", "/api/traffic/metrics"):
        assert p in paths, f"pre-existing route {p} lost"


def test_gateway_config_reads_key(monkeypatch):
    monkeypatch.setenv("TOMTOM_API_KEY", "abc123")
    from gateway.config import GatewayConfig

    cfg = GatewayConfig.from_env()
    assert cfg.tomtom_api_key == "abc123"
    assert cfg.tomtom_enabled is True
    monkeypatch.setenv("TOMTOM_API_KEY", "")
    assert GatewayConfig.from_env().tomtom_enabled is False


# -------------------------------------------------------------- DDL lock-step
def test_migration_0107_additive_and_in_lock_step():
    """0107 must only CREATE new objects, and gateway/traffic_ext must mirror them."""
    from gateway import traffic_ext

    migration = (Path(__file__).resolve().parents[1]
                 / "infra" / "postgres" / "v3" / "0107_traffic_reading.sql").read_text()
    ext_ddl = "\n".join(traffic_ext._DDL)

    for col in ("current_speed", "free_flow_speed", "congestion_level",
                "delay_seconds", "source", "payload", "created_at"):
        assert col in migration, f"column {col} missing from migration 0107"
        assert col in ext_ddl, f"column {col} missing from traffic_ext DDL"
    assert "core.traffic_reading" in migration
    assert "CREATE TABLE IF NOT EXISTS" in migration
    assert "DROP" not in migration.upper()
    assert "ALTER" not in migration.upper()   # additive only — touches nothing existing
