"""Tests for the API gateway + fallback orchestrator (Sub-Criterion 3).

These run in-process via Starlette's TestClient — no docker stack required. The
upstream Vahan services and the Redis cache are stubbed so the four Vahan
fallback rungs can be driven deterministically:

    token present   -> LIVE_PRIMARY   (vahan-live answers)
    token dropped    -> LIVE_FALLBACK  (vahan-sim answers)
    sim stopped      -> CACHED         (only the Redis cache has it)
    cache flushed    -> PROVISIONAL    (+ jnpa.vehicle_master row when PG is up)

The provisional-writeback assertion needs a live Postgres (compose publishes it
on host 5433) and is skipped automatically when it is unreachable.
"""
from __future__ import annotations

import importlib
import os
import socket
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Point the DSN at an unroutable address by default so DB writebacks fail fast
# and silently in the pure in-process suite (the PROVISIONAL DB test overrides).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

PLATE = "MH04AB1234"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code: int, payload: Optional[dict] = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeHttp:
    """Stand-in for the gateway's httpx.AsyncClient.

    ``answers`` maps a URL substring -> (_Resp | Exception). The first matching
    substring wins; an unmatched URL returns a connection error (None upstream).
    """

    def __init__(self, answers: Dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def get(self, url: str, **kw):
        return self._resolve(url)

    async def post(self, url: str, **kw):
        return self._resolve(url)

    async def request(self, method: str, url: str, **kw):
        return self._resolve(url)

    def _resolve(self, url: str):
        self.calls.append(url)
        for needle, val in self.answers.items():
            if needle in url:
                if isinstance(val, Exception):
                    raise val
                return val
        import httpx
        raise httpx.ConnectError(f"no stub for {url}")

    async def aclose(self):
        pass


class FakeCache:
    """In-memory stand-in for gateway.cache (jnpa:cache:{api}:{key})."""

    def __init__(self) -> None:
        self.store: Dict[str, dict] = {}

    def cache_key(self, api: str, key: str) -> str:
        return f"jnpa:cache:{api}:{key}"

    async def put(self, api: str, key: str, value, ttl: int) -> None:
        self.store[self.cache_key(api, key)] = {"value": value, "cached_at": None, "age_s": 1.0}

    async def get(self, api: str, key: str) -> Optional[dict]:
        return self.store.get(self.cache_key(api, key))

    def flush(self) -> None:
        self.store.clear()


# ---------------------------------------------------------------------------
# Harness: build a TestClient with a controllable GatewayState
# ---------------------------------------------------------------------------
def _make_client(*, surepass_token: str, http: FakeHttp, cache: FakeCache):
    """(Re)load the gateway with the given token, then swap in the test doubles.

    We let the app's lifespan build the real GatewayState (so routers' Depends
    resolve normally), then replace its ``http`` client and monkeypatch the
    cache module functions the routers call.
    """
    os.environ["SUREPASS_API_TOKEN"] = surepass_token
    os.environ.setdefault("KAFKA_BROKERS", "127.0.0.1:1")  # pumps will just exit
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()

    import gateway.config as cfgmod
    importlib.reload(cfgmod)
    import gateway.cache as cachemod
    import gateway.routers.vahan as vahanmod
    import gateway.main as mainmod
    importlib.reload(mainmod)

    # Route the routers' module-level `cache` reference at our fake.
    vahanmod.cache.put = cache.put          # type: ignore[assignment]
    vahanmod.cache.get = cache.get          # type: ignore[assignment]

    client = TestClient(mainmod.app)
    client.__enter__()  # run lifespan (builds app.state.gw)
    mainmod.app.state.gw.http = http        # swap in the fake upstream client
    return client, mainmod


def _vahan_live_url(state) -> str:
    return state.cfg.vahan_live_url


# ---------------------------------------------------------------------------
# 1) token present -> LIVE_PRIMARY
# ---------------------------------------------------------------------------
def test_live_primary_when_token_set():
    cache = FakeCache()
    http = FakeHttp({
        "vahan-live": _Resp(200, {"rc_number": PLATE, "blacklist_status": "CLEAR"}),
        "vahan-sim": _Resp(200, {"rc_number": PLATE, "from": "sim"}),
    })
    client, _ = _make_client(surepass_token="tok-123", http=http, cache=cache)
    try:
        r = client.get(f"/api/vahan/rc/{PLATE}")
        assert r.status_code == 200
        body = r.json()
        assert body["decision_path"] == "LIVE_PRIMARY"
        assert any("vahan-live" in c for c in http.calls)
        # And it got cached for a later CACHED rung.
        assert cache.store, "LIVE_PRIMARY response must be cached"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 2) token dropped -> LIVE_FALLBACK
# ---------------------------------------------------------------------------
def test_live_fallback_when_token_absent():
    cache = FakeCache()
    http = FakeHttp({
        "vahan-sim": _Resp(200, {"rc_number": PLATE, "from": "sim"}),
        # vahan-live unmatched -> ConnectError, but it shouldn't even be tried.
    })
    client, mainmod = _make_client(surepass_token="", http=http, cache=cache)
    try:
        assert mainmod.cfg.surepass_enabled is False
        r = client.get(f"/api/vahan/rc/{PLATE}")
        assert r.status_code == 200
        assert r.json()["decision_path"] == "LIVE_FALLBACK"
        # Live should be skipped entirely (no token).
        assert not any("vahan-live" in c for c in http.calls)
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 3) sim stopped -> CACHED
# ---------------------------------------------------------------------------
def test_cached_when_upstreams_down():
    cache = FakeCache()
    # Pre-seed the cache as if a prior good lookup happened.
    import asyncio
    asyncio.get_event_loop_policy().new_event_loop()
    cache.store[cache.cache_key("vahan", PLATE)] = {
        "value": {"rc_number": PLATE, "from": "cache"}, "cached_at": None, "age_s": 12.0,
    }
    import httpx
    http = FakeHttp({
        "vahan-live": httpx.ConnectError("down"),
        "vahan-sim": httpx.ConnectError("down"),
    })
    client, _ = _make_client(surepass_token="tok-123", http=http, cache=cache)
    try:
        r = client.get(f"/api/vahan/rc/{PLATE}")
        assert r.status_code == 200
        body = r.json()
        assert body["decision_path"] == "CACHED"
        assert body["record"]["from"] == "cache"
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# 4) cache flushed -> PROVISIONAL (DB row asserted only if Postgres is up)
# ---------------------------------------------------------------------------
def test_provisional_when_everything_exhausted():
    cache = FakeCache()  # empty
    import httpx
    http = FakeHttp({
        "vahan-live": httpx.ConnectError("down"),
        "vahan-sim": httpx.ConnectError("down"),
    })
    client, _ = _make_client(surepass_token="tok-123", http=http, cache=cache)
    try:
        r = client.get(f"/api/vahan/rc/{PLATE}")
        assert r.status_code == 200
        body = r.json()
        assert body["decision_path"] == "PROVISIONAL"
        assert body["provisional"] is True
        assert "provisional_until" in body["record"]
        assert body.get("alert_id")
    finally:
        client.__exit__(None, None, None)


def _pg_host_dsn() -> Optional[str]:
    dsn = os.environ.get(
        "POSTGRES_TEST_DSN",
        "postgresql+asyncpg://postgres:jnpa_pw@localhost:5433/postgres",
    )
    try:
        with socket.create_connection(("localhost", 5433), timeout=2.0):
            return dsn
    except OSError:
        return None


@pytest.mark.skipif(_pg_host_dsn() is None,
                    reason="Postgres not reachable on localhost:5433; run `make up` first.")
def test_provisional_writes_vehicle_master_row(monkeypatch):
    """PROVISIONAL admission writes a provisional jnpa.vehicle_master row."""
    import asyncio

    dsn = _pg_host_dsn()
    plate = "MH09XY4321"  # valid shape, very unlikely to be pre-verified
    cache = FakeCache()
    import httpx
    http = FakeHttp({
        "vahan-live": httpx.ConnectError("down"),
        "vahan-sim": httpx.ConnectError("down"),
    })
    monkeypatch.setenv("POSTGRES_DSN", dsn)
    client, _ = _make_client(surepass_token="tok-123", http=http, cache=cache)
    try:
        r = client.get(f"/api/vahan/rc/{plate}")
        assert r.status_code == 200
        assert r.json()["decision_path"] == "PROVISIONAL"
    finally:
        client.__exit__(None, None, None)

    from jnpa_shared import db

    async def _row():
        await db.dispose_all()
        row = await db.fetch_one(
            "SELECT plate, provisional, provisional_until FROM jnpa.vehicle_master WHERE plate = :p",
            {"p": plate}, dsn=dsn,
        )
        await db.dispose_all()
        return row

    row = asyncio.run(_row())
    assert row is not None, "provisional vehicle_master row must be written"
    assert row["provisional"] is True
    assert row["provisional_until"] is not None


# ---------------------------------------------------------------------------
# Decision ring buffer: /api/debug/decisions is newest-first
# ---------------------------------------------------------------------------
def test_debug_decisions_newest_first():
    cache = FakeCache()
    http = FakeHttp({"vahan-sim": _Resp(200, {"rc_number": PLATE})})
    client, _ = _make_client(surepass_token="", http=http, cache=cache)
    try:
        client.get(f"/api/vahan/rc/{PLATE}")
        client.get("/api/vahan/rc/MH43CD5678")
        decisions = client.get("/api/debug/decisions").json()
        assert isinstance(decisions, list)
        assert decisions[0]["key"] == "MH43CD5678"  # newest first
        assert decisions[0]["api"] == "vahan"
        assert "decision_path" in decisions[0]
    finally:
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# Trucking-App PWA (Prompt 11): WebPush subscribe + re-route advisory channels
# ---------------------------------------------------------------------------
def test_push_subscribe_and_status():
    """/api/push accepts a subscription and reports it; invalid bodies 422."""
    cache = FakeCache()
    http = FakeHttp({})
    client, _ = _make_client(surepass_token="", http=http, cache=cache)
    try:
        # No VAPID configured in the test env -> key is null, push not configured.
        key = client.get("/api/push/vapid-public-key").json()
        assert key["configured"] is False and key["key"] is None

        ok = client.post("/api/push/subscribe", json={
            "device_id": "DEV-000001",
            "subscription": {"endpoint": "https://example.com/x",
                             "keys": {"p256dh": "a", "auth": "b"}},
        })
        assert ok.status_code == 200 and ok.json()["subscribed"] is True

        bad = client.post("/api/push/subscribe", json={"device_id": "DEV-000001"})
        assert bad.status_code == 422

        status = client.get("/api/push/status").json()
        assert status["subscriptions"] >= 1
        assert "DEV-000001" in status["devices"]
    finally:
        client.__exit__(None, None, None)


def test_reroute_broadcasts_advisory_and_is_pollable():
    """POST /api/trucks/{id}/route emits a `reroute` WS frame, stamps the
    advisory for the polling fallback, and ACK round-trips."""
    cache = FakeCache()
    # The upstream truck-sim accepts the override and returns a route.
    http = FakeHttp({
        "/devices/DEV-000001/route": _Resp(200, {
            "rerouted": True, "device_id": "DEV-000001",
            "dest": {"lat": 18.95, "lon": 72.95}, "route_km": 4.2,
            "state": "EN_ROUTE_TO_PORT",
        }),
    })
    client, _ = _make_client(surepass_token="", http=http, cache=cache)
    try:
        with client.websocket_connect("/api/ws") as ws:
            assert ws.receive_json()["type"] == "hello"
            resp = client.post("/api/trucks/DEV-000001/route", json={"gate_id": "G-JNPCT"})
            body = resp.json()
            assert body["advisory"]["gate_id"] == "G-JNPCT"
            assert body["push_delivered"] is False  # no VAPID in test env

            # Drain frames until the reroute lands (a `decision` frame may precede).
            seen = []
            for _ in range(6):
                f = ws.receive_json()
                seen.append(f["type"])
                if f["type"] == "reroute":
                    assert f["payload"]["device_id"] == "DEV-000001"
                    assert f["payload"]["gate_id"] == "G-JNPCT"
                    break
            assert "reroute" in seen

        # Polling fallback now returns the stored advisory.
        latest = client.get("/api/trucks/DEV-000001/route/latest").json()
        assert latest["advisory"] is not None
        assert latest["advisory"]["gate_id"] == "G-JNPCT"

        # ACK round-trip records a decision and returns the state.
        ack = client.post("/api/trucks/DEV-000001/route/ack", json={"state": "ACK"})
        assert ack.status_code == 200 and ack.json()["state"] == "ACK"
    finally:
        client.__exit__(None, None, None)


# ===========================================================================
# Fault injection (Phase D): forcing a fallback rung on demand
# ===========================================================================
def test_fault_force_vahan_provisional_overrides_live():
    """Forcing vahan->PROVISIONAL yields the cure path even when LIVE answers."""
    cache = FakeCache()
    http = FakeHttp({
        "vahan-live": _Resp(200, {"rc_number": PLATE, "blacklist_status": "CLEAR"}),
        "vahan-sim": _Resp(200, {"rc_number": PLATE, "from": "sim"}),
    })
    client, _ = _make_client(surepass_token="tok-123", http=http, cache=cache)
    try:
        # Baseline: LIVE_PRIMARY answers.
        assert client.get(f"/api/vahan/rc/{PLATE}").json()["decision_path"] == "LIVE_PRIMARY"

        # Force the chain to PROVISIONAL.
        r = client.post("/api/control/fault/vahan", json={"rung": "PROVISIONAL"})
        assert r.status_code == 200
        banner = r.json()["banner"]
        assert banner["active"] is True and banner["severity"] == "RED"
        assert banner["domains"]["vahan"]["forced_rung"] == "PROVISIONAL"

        # Now the same RC call is admitted PROVISIONAL despite a healthy upstream.
        body = client.get(f"/api/vahan/rc/{PLATE}").json()
        assert body["decision_path"] == "PROVISIONAL"
        assert body.get("provisional") is True

        # Clearing restores the real cascade.
        client.delete("/api/control/fault/vahan")
        assert client.get(f"/api/vahan/rc/{PLATE}").json()["decision_path"] == "LIVE_PRIMARY"
    finally:
        client.__exit__(None, None, None)


def test_fault_force_camera_synthetic_flips_card():
    """Forcing camera->SYNTHETIC degrades every camera regardless of frame age."""
    cache = FakeCache()
    http = FakeHttp({})
    client, _ = _make_client(surepass_token="", http=http, cache=cache)
    try:
        client.post("/api/control/fault/camera", json={"rung": "SYNTHETIC"})
        cams = client.get("/api/anpr/cameras").json()["cameras"]
        assert cams and all(c["decision_path"] == "SYNTHETIC" for c in cams)
        assert all(c["forced"] for c in cams)
    finally:
        client.__exit__(None, None, None)


def test_fault_control_validation_and_clear_all():
    cache = FakeCache()
    client, _ = _make_client(surepass_token="", http=FakeHttp({}), cache=cache)
    try:
        # Unknown domain -> 404.
        assert client.post("/api/control/fault/nope", json={"rung": "X"}).status_code == 404
        # Invalid rung for a real domain -> 422.
        assert client.post("/api/control/fault/trucks", json={"rung": "PROVISIONAL"}).status_code == 422
        # Valid force, then clear-all resets the banner.
        client.post("/api/control/fault/trucks", json={"rung": "TERTIARY"})
        snap = client.get("/api/control/fault").json()["domains"]
        assert snap["trucks"]["forced_rung"] == "TERTIARY"
        banner = client.delete("/api/control/fault").json()["banner"]
        assert banner["active"] is False
    finally:
        client.__exit__(None, None, None)
