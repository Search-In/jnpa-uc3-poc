"""ULIP Logistics Intelligence integration tests (no DB / no network).

Covers the required scenarios (same pattern as tests/test_tomtom.py):
  * successful login + fetch          (client, httpx.MockTransport)
  * token caching / single re-login   (401 -> one forced re-login, then
                                       UlipAuthError)
  * authentication failure            (login rejected -> UlipAuthError)
  * timeout                           (client retries then raises UlipTimeout)
  * 5xx handling                      (retried with backoff, then typed error)
  * rate limit (429)                  (fail fast, no retry)
  * invalid response                  (non-JSON body / API-level error envelope)
  * credential redaction              (secret never in exception text)
  * normalization                     (FASTAG txns / LDB movements -> events)
  * service fallback                  (LIVE -> CACHED -> DATABASE -> FALLBACK;
                                       a ULIP outage never breaks the API and
                                       the FALLBACK rung is explicitly EMPTY —
                                       no fabricated shipment data)
plus the additive migration 0109 / gateway/logistics_ext DDL lock-step and
the router/config posture.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from integrations.ulip import (
    UlipAuthError,
    UlipClient,
    UlipEnvelope,
    UlipHTTPError,
    UlipInvalidResponse,
    UlipNotConfigured,
    UlipTimeout,
    normalize_container_events,
    normalize_vehicle_events,
)
from services.logistics import LogisticsService
from services.logistics import service as logistics_service


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- canned payloads
def _login_payload() -> dict:
    return {"error": "false", "code": "200", "message": "SUCCESS",
            "response": {"id": "issued-token-abc", "username": "jnpa_uc3"}}


def _fastag_payload() -> dict:
    """A representative ULIP FASTAG answer (NPCI txn list shape)."""
    return {
        "error": "false", "code": "200", "message": "SUCCESS",
        "response": [{
            "response": {
                "vehicle": {
                    "vehltxnList": {
                        "totalTagsInMsg": "2",
                        "txn": [
                            {"readerReadTime": "2026-07-29 06:15:29.0",
                             "seqNo": "1",
                             "tollPlazaName": "Karal Phata Toll Plaza",
                             "tollPlazaGeocode": "18.842,73.041",
                             "laneDirection": "N",
                             "vehicleType": "VC10",
                             "vehicleRegNo": "MH46AB1234"},
                            {"readerReadTime": "2026-07-28 22:03:11.0",
                             "seqNo": "2",
                             "tollPlazaName": "Palaspe Toll Plaza",
                             "tollPlazaGeocode": "18.988,73.109",
                             "laneDirection": "S",
                             "vehicleType": "VC10",
                             "vehicleRegNo": "MH46AB1234"},
                        ],
                    }
                }
            }
        }],
    }


def _ldb_payload() -> dict:
    """A representative ULIP LDB container-tracking answer."""
    return {
        "error": "false", "code": "200", "message": "SUCCESS",
        "response": [{
            "response": {
                "containerNumber": "MSKU1234565",
                "movements": [
                    {"event": "GATE_IN", "location": "JNPA Gate-3",
                     "eventTime": "2026-07-29 04:10:00"},
                    {"event": "YARD", "location": "NSICT Yard Block-B",
                     "eventTime": "2026-07-29 05:40:00"},
                ],
            }
        }],
    }


def _client_with(handler, **kwargs) -> UlipClient:
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("client_id", "jnpa_uc3")
    kwargs.setdefault("client_secret", "sekret-pass-123")
    kwargs.setdefault("api_key", "")
    return UlipClient(http_client=httpx.AsyncClient(transport=transport),
                      retries=2, backoff_s=0.0, **kwargs)


def _login_aware(api_handler):
    """Wrap an API handler with the standard /user/login answer."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        return api_handler(request)
    return handler


# ------------------------------------------------------------------ client: happy
def test_client_vehicle_movement_success():
    calls = {"login": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            calls["login"] += 1
            body = request.read().decode()
            assert "jnpa_uc3" in body
            return httpx.Response(200, json=_login_payload())
        calls["api"] += 1
        assert request.url.path.endswith("/FASTAG/01")
        assert request.headers["Authorization"] == "Bearer issued-token-abc"
        assert b"MH46AB1234" in request.read()
        return httpx.Response(200, json=_fastag_payload())

    env = _run(_client_with(handler).fetch_vehicle_movement("mh46ab1234"))
    assert env.ok
    events = normalize_vehicle_events(env, "MH46AB1234")
    assert len(events) == 2
    newest = events[0]                       # sorted newest first
    assert newest["event_type"] == "TOLL_CROSSING"
    assert newest["ref_type"] == "VEHICLE"
    assert newest["ref_id"] == "MH46AB1234"
    assert newest["location"] == "Karal Phata Toll Plaza"
    assert newest["latitude"] == 18.842
    assert newest["longitude"] == 73.041
    assert newest["source_api"] == "FASTAG"
    assert newest["event_ts"].startswith("2026-07-29T06:15:29")
    assert calls == {"login": 1, "api": 1}


def test_client_container_tracking_success():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/LDB/01")
        assert b"MSKU1234565" in request.read()
        return httpx.Response(200, json=_ldb_payload())

    env = _run(_client_with(_login_aware(handler)).fetch_container_tracking("MSKU1234565"))
    events = normalize_container_events(env, "MSKU1234565")
    assert len(events) == 2
    newest = events[0]
    assert newest["event_type"] == "CONTAINER_MOVEMENT"
    assert newest["ref_type"] == "CONTAINER"
    assert newest["location"] == "NSICT Yard Block-B"
    assert newest["source_api"] == "LDB"
    assert newest["detail"]["event"] == "YARD"


def test_client_static_key_skips_login():
    calls = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            calls["login"] += 1
            return httpx.Response(200, json=_login_payload())
        assert request.headers["Authorization"] == "Bearer static-key-9"
        return httpx.Response(200, json=_fastag_payload())

    client = _client_with(handler, api_key="static-key-9",
                          client_id="", client_secret="")
    assert client.auth_mode == "static"
    _run(client.fetch_vehicle_movement("MH46AB1234"))
    assert calls["login"] == 0


def test_client_token_cached_across_calls():
    calls = {"login": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            calls["login"] += 1
            return httpx.Response(200, json=_login_payload())
        return httpx.Response(200, json=_fastag_payload())

    client = _client_with(handler)
    _run(client.fetch_vehicle_movement("MH46AB1234"))
    _run(client.fetch_vehicle_movement("MH46AB1234"))
    assert calls["login"] == 1               # token reused within its TTL


# --------------------------------------------------------- normalization details
def test_envelope_ok_accepts_string_and_bool_spellings():
    assert UlipEnvelope.model_validate(
        {"error": "false", "code": "200", "response": []}).ok
    assert UlipEnvelope.model_validate(
        {"error": False, "code": 200, "response": []}).ok
    assert not UlipEnvelope.model_validate(
        {"error": "true", "code": "500", "message": "boom"}).ok
    assert not UlipEnvelope.model_validate(
        {"error": "false", "code": "404"}).ok


def test_normalize_vehicle_events_tolerates_flat_shape():
    env = UlipEnvelope.model_validate({
        "error": "false", "code": "200",
        "response": [{"readerReadTime": "2026-07-29 01:00:00",
                      "tollPlazaName": "Somewhere"}],
    })
    events = normalize_vehicle_events(env, "MH46AB1234")
    assert len(events) == 1
    assert events[0]["location"] == "Somewhere"


def test_normalize_handles_empty_response():
    env = UlipEnvelope.model_validate({"error": "false", "code": "200",
                                       "response": []})
    assert normalize_vehicle_events(env, "MH46AB1234") == []
    assert normalize_container_events(env, "MSKU1234565") == []


# ---------------------------------------------------------------- client: failure
def test_client_not_configured_raises():
    client = UlipClient(api_key="", client_id="", client_secret="")
    assert not client.configured
    with pytest.raises(UlipNotConfigured):
        _run(client.fetch_vehicle_movement("MH46AB1234"))


def test_client_login_rejected_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/user/login")
        return httpx.Response(401, json={"error": "true", "message": "bad creds"})

    with pytest.raises(UlipAuthError):
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))


def test_client_expired_token_relogs_in_once():
    calls = {"login": 0, "api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            calls["login"] += 1
            return httpx.Response(200, json=_login_payload())
        calls["api"] += 1
        if calls["api"] == 1:
            return httpx.Response(401, json={"message": "token expired"})
        return httpx.Response(200, json=_fastag_payload())

    env = _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert env.ok
    assert calls["login"] == 2               # initial + one forced re-login
    assert calls["api"] == 2


def test_client_persistent_401_raises_auth_error():
    calls = {"api": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        calls["api"] += 1
        return httpx.Response(403, json={"message": "subscription inactive"})

    with pytest.raises(UlipAuthError):
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert calls["api"] == 2                 # once + once after the re-login


def test_client_timeout_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        calls["n"] += 1
        raise httpx.ReadTimeout("boom", request=request)

    with pytest.raises(UlipTimeout):
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert calls["n"] == 3                   # first try + 2 retries


def test_client_5xx_retries_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        calls["n"] += 1
        return httpx.Response(503, json={"message": "overloaded"})

    with pytest.raises(UlipHTTPError) as exc:
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert exc.value.status_code == 503
    assert calls["n"] == 3


def test_client_rate_limit_fails_fast():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        calls["n"] += 1
        return httpx.Response(429, json={"message": "rate limit exceeded"})

    with pytest.raises(UlipHTTPError) as exc:
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert exc.value.status_code == 429
    assert exc.value.is_rate_limited
    assert calls["n"] == 1                   # retrying would burn the budget


def test_client_invalid_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(UlipInvalidResponse):
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))


def test_client_api_level_error_envelope_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/user/login"):
            return httpx.Response(200, json=_login_payload())
        return httpx.Response(200, json={"error": "true", "code": "500",
                                         "message": "source system down"})

    with pytest.raises(UlipInvalidResponse):
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))


def test_client_secret_never_in_exception_text():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(Exception) as exc:
        _run(_client_with(handler).fetch_vehicle_movement("MH46AB1234"))
    assert "sekret-pass-123" not in str(exc.value)


# --------------------------------------------------------------- service fixtures
class _StubUlipClient:
    """UlipClient stand-in with success/failure/configured switches."""

    def __init__(self, ok: bool = True, configured: bool = True) -> None:
        self.ok = ok
        self._configured = configured
        self.api_url = "https://ulip.example/v1"
        self.auth_mode = "login"
        self.fastag_api = "FASTAG/01"
        self.ldb_api = "LDB/01"
        self.timeout_s = 5.0
        self.retries = 2

    @property
    def configured(self) -> bool:
        return self._configured

    async def fetch_vehicle_movement(self, vehicle_number: str) -> UlipEnvelope:
        if not self.ok:
            raise UlipTimeout("ulip down")
        return UlipEnvelope.model_validate(_fastag_payload())

    async def fetch_container_tracking(self, container_number: str) -> UlipEnvelope:
        if not self.ok:
            raise UlipTimeout("ulip down")
        return UlipEnvelope.model_validate(_ldb_payload())


class _StubRepo:
    def __init__(self, tracking=None, events=None) -> None:
        self.tracking = tracking
        self.events = events or []
        self.inserted_events: list[dict] = []
        self.upserts: list[dict] = []
        self.audits: list[dict] = []

    async def insert_events(self, events):
        self.inserted_events.extend(events)
        return len(events)

    async def list_events(self, **kwargs):
        return self.events

    async def count_events(self, **kwargs):
        return len(self.events)

    async def upsert_tracking(self, **kwargs):
        self.upserts.append(kwargs)
        return len(self.upserts)

    async def get_tracking(self, ref_id):
        return self.tracking

    async def list_tracking(self, **kwargs):
        return [self.tracking] if self.tracking else []

    async def summary(self, **kwargs):
        return {"event_count": len(self.events),
                "vehicle_count": 1 if self.events else 0,
                "container_count": 0,
                "last_event_ts": None,
                "events_by_type": {"TOLL_CROSSING": len(self.events)}
                                  if self.events else {}}

    async def insert_audit(self, **kwargs):
        self.audits.append(kwargs)
        return len(self.audits)

    async def last_audit(self):
        return self.audits[-1] if self.audits else None


@pytest.fixture()
def fake_cache(monkeypatch):
    store: dict[str, dict] = {}

    async def put(key, value, ttl):
        store[key] = {"value": value, "cached_at": "2026-07-29T06:00:00+00:00"}

    async def get(key):
        raw = store.get(key)
        if raw is None:
            return None
        return {"value": raw["value"], "cached_at": raw["cached_at"], "age_s": 60.0}

    monkeypatch.setattr(logistics_service, "_cache_put", put)
    monkeypatch.setattr(logistics_service, "_cache_get", get)
    return store


def _service(client, repo=None) -> LogisticsService:
    return LogisticsService(client=client, repository=repo or _StubRepo())


# ------------------------------------------------------------------ service: live
def test_service_tracking_live(fake_cache):
    repo = _StubRepo()
    out = _run(_service(_StubUlipClient(), repo).tracking("mh46ab1234"))

    assert out["status"] == "LIVE"
    assert out["source"] == "ULIP"
    assert out["decision_path"] == "LIVE"
    t = out["tracking"]
    assert t["ref_id"] == "MH46AB1234"
    assert t["ref_type"] == "VEHICLE"
    assert t["event_count"] == 2
    assert t["last_location"] == "Karal Phata Toll Plaza"
    assert t["data_available"] is True
    # Raw upstream detail stays server-side — the API events are normalised.
    assert "detail" not in t["events"][0]
    # Persisted: events + snapshot + a success audit row.
    assert len(repo.inserted_events) == 2
    assert repo.upserts and repo.upserts[0]["ref_id"] == "MH46AB1234"
    assert repo.audits and repo.audits[0]["ok"] is True
    # Cache write-back happened.
    assert any("tracking:MH46AB1234" in k for k in fake_cache)


def test_service_container_ref_routed_to_ldb(fake_cache):
    out = _run(_service(_StubUlipClient()).tracking("MSKU1234565"))
    assert out["tracking"]["ref_type"] == "CONTAINER"
    assert out["tracking"]["events"][0]["source_api"] == "LDB"


# --------------------------------------------------------- service: fallback chain
def test_service_cache_fallback(fake_cache):
    """ULIP LIVE once, then down -> the answer replays from the Redis rung."""
    _run(_service(_StubUlipClient()).tracking("MH46AB1234"))        # primes cache
    out = _run(_service(_StubUlipClient(ok=False)).tracking("MH46AB1234"))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "ULIP_CACHE"
    assert out["decision_path"] == "CACHED"
    assert out["tracking"]["event_count"] == 2                      # last good answer
    assert out["cache_age_s"] == 60.0


def test_service_db_fallback(fake_cache):
    """Redis empty -> the persisted snapshot + events answer."""
    snapshot = {"ref_type": "VEHICLE", "ref_id": "MH46AB1234",
                "status": "IN_TRANSIT", "last_event": "TOLL_CROSSING",
                "last_location": "Palaspe Toll Plaza",
                "last_event_ts": "2026-07-28T22:03:11+00:00", "event_count": 2}
    events = [{"ref_type": "VEHICLE", "ref_id": "MH46AB1234",
               "event_type": "TOLL_CROSSING",
               "event_ts": "2026-07-28T22:03:11+00:00",
               "location": "Palaspe Toll Plaza", "latitude": 18.988,
               "longitude": 73.109, "source": "ULIP", "source_api": "FASTAG"}]
    repo = _StubRepo(tracking=snapshot, events=events)
    out = _run(_service(_StubUlipClient(ok=False), repo).tracking("MH46AB1234"))

    assert out["status"] == "DEGRADED"
    assert out["source"] == "ULIP_DB"
    assert out["decision_path"] == "DATABASE"
    assert out["tracking"]["last_location"] == "Palaspe Toll Plaza"
    assert out["tracking"]["data_available"] is True
    # The failed LIVE attempt was still audited.
    assert repo.audits and repo.audits[0]["ok"] is False


def test_service_fallback_is_empty_never_fabricated(fake_cache):
    """Nothing live/cached/persisted -> an explicitly EMPTY answer. The
    logistics surface must NEVER invent shipment data (unlike the synthetic
    rungs of the weather/traffic surfaces)."""
    out = _run(_service(_StubUlipClient(ok=False)).tracking("MH46AB1234"))

    assert out["status"] == "OFFLINE"                # never a hard failure
    assert out["source"] == "NONE"
    assert out["decision_path"] == "FALLBACK"
    assert out["tracking"]["data_available"] is False
    assert out["tracking"]["events"] == []           # no fabricated events
    assert out["tracking"]["event_count"] == 0
    assert out["tracking"]["tracking_status"] == "UNKNOWN"


def test_service_unconfigured_never_breaks(fake_cache):
    """No credentials -> the LIVE rung is skipped, the surface still answers."""
    out = _run(_service(_StubUlipClient(configured=False)).tracking("MH46AB1234"))

    assert out["status"] == "OFFLINE"
    assert out["source"] == "NONE"
    assert out["tracking"]["data_available"] is False


# ------------------------------------------------------------------ service: misc
def test_service_current_summary(fake_cache):
    events = [{"ref_type": "VEHICLE", "ref_id": "MH46AB1234",
               "event_type": "TOLL_CROSSING",
               "event_ts": "2026-07-29T06:15:29+00:00",
               "location": "Karal Phata Toll Plaza", "latitude": 18.842,
               "longitude": 73.041, "source": "ULIP", "source_api": "FASTAG"}]
    repo = _StubRepo(events=events)
    out = _run(_service(_StubUlipClient(), repo).current())

    assert out["decision_path"] == "DATABASE"
    assert out["logistics"]["event_count"] == 1
    assert out["logistics"]["events_by_type"] == {"TOLL_CROSSING": 1}
    assert out["logistics"]["data_available"] is True
    assert out["ulip"]["configured"] is True
    # No successful ULIP call yet in this window -> DEGRADED, not LIVE.
    assert out["status"] == "DEGRADED"


def test_service_current_empty_is_offline(fake_cache):
    out = _run(_service(_StubUlipClient(ok=False)).current())
    assert out["status"] == "OFFLINE"
    assert out["logistics"]["data_available"] is False
    assert out["logistics"]["latest_events"] == []


def test_service_health_posture(fake_cache):
    out = _run(_service(_StubUlipClient()).health())
    assert out["system"] == "LOGISTICS"
    assert out["provider"] == "ULIP"
    assert out["configured"] is True
    assert out["auth_mode"] == "login"
    assert out["apis"] == {"vehicle": "FASTAG/01", "container": "LDB/01"}
    # No credential material in the posture.
    assert "client_secret" not in str(out)
    assert "api_key" not in out


def test_classify_ref():
    assert logistics_service.classify_ref("MSKU1234565") == "CONTAINER"
    assert logistics_service.classify_ref("msku1234565") == "CONTAINER"
    assert logistics_service.classify_ref("MH46AB1234") == "VEHICLE"
    assert logistics_service.classify_ref("MH-46-AB-1234") == "VEHICLE"


# ------------------------------------------------------------------ router / config
def test_router_exposes_required_routes():
    from gateway.routers import logistics as lg

    paths = {r.path for r in lg.router.routes}
    for p in ("/api/logistics/health", "/api/logistics/current",
              "/api/logistics/tracking/{ref_id}", "/api/logistics/events"):
        assert p in paths, f"missing route {p}"


def test_existing_ulip_fastag_ldb_routes_untouched():
    """Additive only — the three pre-existing ULIP touchpoints keep their routes."""
    from gateway.routers import fastag, ldb, ulip

    assert {r.path for r in ulip.router.routes} == {"/api/ulip/proxy/{device_id}"}
    fastag_paths = {r.path for r in fastag.router.routes}
    for p in ("/api/fastag/balance", "/api/fastag/toll-enroute",
              "/api/fastag/transactions", "/api/fastag/health"):
        assert p in fastag_paths, f"pre-existing route {p} lost"
    ldb_paths = {r.path for r in ldb.router.routes}
    for p in ("/api/ldb/container/{container_number}", "/api/ldb/health"):
        assert p in ldb_paths, f"pre-existing route {p} lost"


def test_gateway_config_reads_credentials(monkeypatch):
    from gateway.config import GatewayConfig

    monkeypatch.setenv("ULIP_API_URL", "https://ulip.example/v1")
    monkeypatch.setenv("ULIP_CLIENT_ID", "acct")
    monkeypatch.setenv("ULIP_CLIENT_SECRET", "pw")
    monkeypatch.setenv("ULIP_API_KEY", "")
    cfg = GatewayConfig.from_env()
    assert cfg.ulip_api_url == "https://ulip.example/v1"
    assert cfg.ulip_client_id == "acct"
    assert cfg.ulip_logistics_enabled is True

    monkeypatch.setenv("ULIP_CLIENT_ID", "")
    monkeypatch.setenv("ULIP_CLIENT_SECRET", "")
    assert GatewayConfig.from_env().ulip_logistics_enabled is False

    monkeypatch.setenv("ULIP_API_KEY", "static-key")
    assert GatewayConfig.from_env().ulip_logistics_enabled is True


# -------------------------------------------------------------- DDL lock-step
def test_migration_0109_additive_and_in_lock_step():
    """0109 must only CREATE new objects, and gateway/logistics_ext must mirror them."""
    from gateway import logistics_ext

    migration = (Path(__file__).resolve().parents[1]
                 / "infra" / "postgres" / "v3" / "0109_logistics_ulip.sql").read_text()
    ext_ddl = "\n".join(logistics_ext._DDL)

    for table in ("core.logistics_event", "core.logistics_tracking",
                  "core.ulip_api_audit"):
        assert table in migration, f"table {table} missing from migration 0109"
        assert table in ext_ddl, f"table {table} missing from logistics_ext DDL"
    for col in ("ref_type", "ref_id", "event_type", "event_ts", "location",
                "latitude", "longitude", "source_api", "detail",
                "last_event_ts", "event_count", "api_name", "http_status",
                "latency_ms", "created_at"):
        assert col in migration, f"column {col} missing from migration 0109"
        assert col in ext_ddl, f"column {col} missing from logistics_ext DDL"
    assert "CREATE TABLE IF NOT EXISTS" in migration
    assert "DROP" not in migration.upper()
    assert "ALTER" not in migration.upper()   # additive only — touches nothing existing
