"""Regression tests for the four reported live-demo failures.

Each test names the failure it pins, so a future change that reintroduces the bug
fails with the symptom the demo actually showed rather than an abstract assertion.

  1. "Driver advisory APIs fail after ~30 seconds"
     -> gateway/routers/trucks.py `_primary` re-raised every transport error, so a
        truck-sim ReadTimeout became an HTTP 500 and bypassed the documented
        PRIMARY -> SECONDARY -> TERTIARY fallback ladder entirely.
  2. "Loaded data disappears after refresh"
     -> re-route advisories lived only in the module-level LAST_REROUTE dict.
  3. Driver A must never receive Driver B's data
     -> `violation_enforced` was broadcast unaddressed carrying plate, driver name,
        fine and challan number; `reroute_ack` likewise carried a device_id.
  4. Driver REST scoping with AUTH_ENABLED=false
     -> /api/driver/jobs returned {} scope, i.e. every driver's jobs.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest


# ------------------------------------------------------- 1. fallback ladder
class _FakeHttp:
    def __init__(self, behaviour) -> None:
        self._behaviour = behaviour
        self.calls: list[str] = []

    async def get(self, url, **kw):
        self.calls.append(url)
        return self._behaviour(url)


class _FakeFaults:
    def forced(self, _api):
        return None


class _FakeCfg:
    truck_api_url = "http://truck-sim:8000"
    port = 8000
    postgres_dsn = None
    gate_boom_delay_s = 30


class _FakeState:
    def __init__(self, http) -> None:
        self.http = http
        self.cfg = _FakeCfg()
        self.faults = _FakeFaults()
        self.decisions: list[dict] = []

    async def record_decision(self, **kw):
        self.decisions.append(kw)


@pytest.mark.asyncio
async def test_truck_sim_timeout_degrades_instead_of_500ing():
    """A dead upstream must walk the ladder, not error.

    This is the "driver advisory fails after 30 s" bug: a 12 s ReadTimeout was
    re-raised as a 500, and the client's retries made it look like a 30 s hang.
    """
    from gateway.routers import trucks

    def _timeout(_url):
        raise httpx.ReadTimeout("upstream did not respond")

    state = _FakeState(_FakeHttp(_timeout))
    # _primary must swallow-and-log so the caller can fall through.
    assert await trucks._primary(state, "TRK-000001") is None


@pytest.mark.asyncio
async def test_truck_sim_connect_error_degrades():
    from gateway.routers import trucks

    def _refused(_url):
        raise httpx.ConnectError("connection refused")

    state = _FakeState(_FakeHttp(_refused))
    assert await trucks._primary(state, "TRK-000001") is None


@pytest.mark.asyncio
async def test_malformed_upstream_body_degrades():
    from gateway.routers import trucks

    class _BadJson:
        status_code = 200
        headers: dict = {}
        text = "<html>gateway error</html>"

        def json(self):
            raise ValueError("not json")

    state = _FakeState(_FakeHttp(lambda _u: _BadJson()))
    assert await trucks._primary(state, "TRK-000001") is None


@pytest.mark.asyncio
async def test_primary_still_returns_a_good_record():
    """The fix must not change the happy path."""
    from gateway.routers import trucks

    class _Ok:
        status_code = 200
        headers: dict = {}
        text = '{"device_id": "TRK-000001", "plate": "MH43BX1488"}'

        def json(self):
            return {"device_id": "TRK-000001", "plate": "MH43BX1488"}

    state = _FakeState(_FakeHttp(lambda _u: _Ok()))
    rec = await trucks._primary(state, "TRK-000001")
    assert rec["plate"] == "MH43BX1488"


# --------------------------------------------------- 2. advisory durability
class _FakeAdvisoryRepo:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    async def save(self, device_id, advisory):
        self.rows[device_id] = {**dict(advisory), "ack_state": None}
        return True

    async def ack(self, device_id, state_val):
        if device_id in self.rows:
            self.rows[device_id]["ack_state"] = state_val
            return True
        return False

    async def latest(self, device_id):
        return self.rows.get(device_id)


@pytest.mark.asyncio
async def test_advisory_survives_a_gateway_restart(monkeypatch):
    """"Loaded data disappears after refresh": the advisory now comes back from RDS."""
    from gateway.routers import trucks

    repo = _FakeAdvisoryRepo()
    monkeypatch.setattr(trucks, "_advisory_repo", lambda gw: repo)
    trucks.LAST_REROUTE.clear()

    advisory = {"type": "reroute", "device_id": "TRK-000001", "gate_id": "G-JNPCT",
                "ts": "2026-08-03T10:00:00Z"}
    await repo.save("TRK-000001", advisory)

    # A restart empties the in-memory cache...
    trucks.LAST_REROUTE.clear()
    out = await trucks.latest_reroute("TRK-000001", gw=object())

    # ...but the driver still gets the banner, served from RDS.
    assert out["advisory"]["gate_id"] == "G-JNPCT"
    assert out["source"] == "rds"
    # And the cache is re-warmed so the next poll is served from memory.
    assert "TRK-000001" in trucks.LAST_REROUTE


@pytest.mark.asyncio
async def test_unknown_device_has_no_advisory(monkeypatch):
    from gateway.routers import trucks

    monkeypatch.setattr(trucks, "_advisory_repo", lambda gw: _FakeAdvisoryRepo())
    trucks.LAST_REROUTE.clear()
    out = await trucks.latest_reroute("TRK-999999", gw=object())
    assert out["advisory"] is None
    assert out["source"] is None


# --------------------------------------------------- 3. notification leaks
@pytest.mark.asyncio
async def test_reroute_ack_is_addressed_to_the_acking_driver(monkeypatch):
    """Driver B must not receive Driver A's ACK frame."""
    from gateway.routers import trucks

    frames: list[tuple[str, Any, Optional[str]]] = []

    class _Ws:
        async def broadcast(self, type_, payload, *, device_id=None):
            frames.append((type_, payload, device_id))

    class _Gw:
        ws = _Ws()
        cfg = _FakeCfg()

        async def record_decision(self, **kw):
            return None

    # An ACK now requires an advisory to acknowledge (audit fix T-2), so give the
    # driver one first. Previously any ACK succeeded, which let the evidence trail
    # record a push -> driver -> ACK round-trip that had never happened.
    repo = _FakeAdvisoryRepo()
    monkeypatch.setattr(trucks, "_advisory_repo", lambda gw: repo)
    trucks.LAST_REROUTE.clear()
    await repo.save("TRK-000001", {"type": "reroute", "device_id": "TRK-000001"})

    await trucks.ack_reroute("TRK-000001", body={"state": "ACK"}, gw=_Gw())

    assert len(frames) == 1
    type_, payload, device_id = frames[0]
    assert type_ == "reroute_ack"
    assert device_id == "TRK-000001"       # ADDRESSED, not a broadcast
    assert payload["device_id"] == "TRK-000001"


def test_violation_enforced_is_addressed_when_the_driver_is_known():
    """The PII leak: plate / driver / fine / challan reached every PWA socket.

    The payload carries no device_id, so the client-side `isForOtherDevice` filter
    could not drop it — the data arrived on every driver's socket even though the
    notification stayed silent. It must be addressed at the SERVER.
    """
    import inspect

    from gateway.routers import violations

    src = inspect.getsource(violations)
    # The frame is addressed with the resolved device, and the device is resolved
    # BEFORE the broadcast (not after, as it used to be).
    assert 'await state.ws.broadcast("violation_enforced", notification, device_id=device_id)' in src
    broadcast_at = src.index('broadcast("violation_enforced"')
    resolve_at = src.index("device_id = await push.resolve_device")
    assert resolve_at < broadcast_at, "the device must be resolved before the fan-out"


def test_ws_hub_fails_closed_for_an_unidentified_driver():
    """A socket known to be a DRIVER but not yet bound receives no addressed frame."""
    from gateway.ws import WsHub

    hub = WsHub()
    sock = object()
    hub._identity[sock] = {"device_id": None, "role": "DRIVER"}
    assert hub._wants(sock, "TRK-000001") is False
    # A control-room socket keeps seeing everything.
    dash = object()
    hub._identity[dash] = {"device_id": None, "role": "CONTROL_ROOM"}
    assert hub._wants(dash, "TRK-000001") is True
    # A bound driver socket sees only its own device.
    a = object()
    hub._identity[a] = {"device_id": "TRK-000001", "role": "DRIVER"}
    assert hub._wants(a, "TRK-000001") is True
    assert hub._wants(a, "TRK-000002") is False


# ------------------------------------------------- 4. driver REST scoping
@pytest.mark.asyncio
async def test_driver_jobs_are_scoped_by_device_header_when_auth_is_off(monkeypatch):
    """With AUTH_ENABLED=false the PWA's device header must still scope the caller.

    Before the fix `_scope` returned {} here, so any driver's PWA could list and
    act on every other driver's jobs over REST — the same leak the WebSocket fix
    closed on the push side.
    """
    from gateway.routers import driver_jobs

    monkeypatch.setattr(driver_jobs, "auth_enabled", lambda: False)

    class _Req:
        def __init__(self, headers=None, query=None):
            self.headers = headers or {}
            self.query_params = query or {}
            self.state = type("S", (), {"principal": None})()

    scoped = await driver_jobs._scope(_Req({"X-Device-Id": "TRK-000001"}), svc=None)
    assert scoped["vehicle_id"] == "TRK-000001"

    # The query-param form works too (the web PWA variant).
    scoped_q = await driver_jobs._scope(_Req(query={"device_id": "TRK-000002"}), svc=None)
    assert scoped_q["vehicle_id"] == "TRK-000002"

    # No device at all -> unscoped, preserving control-room/back-office access.
    assert await driver_jobs._scope(_Req(), svc=None) == {}


def test_ownership_check_rejects_another_drivers_job():
    from gateway.routers import driver_jobs

    scope = {"vehicle_id": "TRK-000001", "vehicle_plate": "TRK000001"}
    mine = {"vehicle_id": "TRK-000001", "vehicle_no": "MH43BX1488"}
    theirs = {"vehicle_id": "TRK-000002", "vehicle_no": "MH43CQ2814"}
    assert driver_jobs._owns(scope, mine) is True
    assert driver_jobs._owns(scope, theirs) is False
