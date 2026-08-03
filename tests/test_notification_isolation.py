"""Driver notification ISOLATION — driver A's advisory must never reach driver B.

Regression suite for the notification-targeting audit. The bug: ``dispatch()``
resolved a specific ``device_id`` and then called ``ws.broadcast()``, which fanned
the frame out to EVERY socket; the PWA compounded it by accepting any alert that
carried no address. Every driver therefore saw every other driver's advisory.

What is asserted here, bottom-up:

  1. ``WsHub``     — an addressed frame reaches only the matching driver socket,
                     while control-room (dashboard) sockets still see everything.
  2. ``dispatch``  — the dispatcher addresses the WS leg and stamps ``audience``.
  3. end-to-end    — two real ``/api/ws`` connections + the real
                     ``POST /api/ai/event`` path: A receives, B does not.

The end-to-end case proves NON-delivery without timeouts by interleaving events:
A gets one, B gets one, A gets another. Reading two frames off A must yield A's
two events — if B's event leaked into A's stream the second read fails. The same
holds symmetrically for B.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

DEVICE_A = "TRK-000001"
DEVICE_B = "TRK-000002"


# --------------------------------------------------------------------- fakes
class _FakeSocket:
    """Minimal stand-in for a starlette WebSocket (accept + send_json)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, message) -> None:
        self.sent.append(message)

    def types(self) -> list[str]:
        return [m["type"] for m in self.sent]

    def bodies(self) -> list:
        return [m["payload"].get("body") for m in self.sent]


class _FakeGw:
    def __init__(self, hub) -> None:
        self.ws = hub


# ------------------------------------------------------------------- WsHub
def test_addressed_frame_reaches_only_the_matching_driver():
    """A frame addressed to A reaches A + the dashboard, never B."""
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a, b, dash = _FakeSocket("A"), _FakeSocket("B"), _FakeSocket("dash")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.connect(b, device_id=DEVICE_B, role="DRIVER")
        await hub.connect(dash)  # control room: no device binding
        await hub.broadcast("alert", {"body": "for-A"}, device_id=DEVICE_A)
        return a, b, dash

    a, b, dash = asyncio.run(run())
    assert a.bodies() == ["for-A"]      # driver A receives it
    assert b.sent == []                 # driver B receives NOTHING
    assert dash.bodies() == ["for-A"]   # control room unaffected


def test_unaddressed_frame_still_reaches_everyone():
    """A genuine broadcast (no device_id) keeps its historical fan-out."""
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a, b, dash = _FakeSocket("A"), _FakeSocket("B"), _FakeSocket("dash")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.connect(b, device_id=DEVICE_B, role="DRIVER")
        await hub.connect(dash)
        await hub.broadcast("traffic", {"body": "corridor"})
        return a, b, dash

    a, b, dash = asyncio.run(run())
    assert a.bodies() == b.bodies() == dash.bodies() == ["corridor"]


def test_identify_binds_an_anonymous_socket():
    """AUTH_ENABLED=false: the PWA's identify frame is what makes isolation work."""
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a, b = _FakeSocket("A"), _FakeSocket("B")
        await hub.connect(a)  # anonymous (demo profile, no token)
        await hub.connect(b)
        await hub.identify(a, device_id=DEVICE_A)
        await hub.identify(b, device_id=DEVICE_B)
        await hub.broadcast("alert", {"body": "for-B"}, device_id=DEVICE_B)
        return a, b

    a, b = asyncio.run(run())
    assert a.sent == []
    assert b.bodies() == ["for-B"]


def test_identify_cannot_repoint_an_authenticated_socket():
    """A verified JWT binding wins; a client may not re-address its socket."""
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a = _FakeSocket("A")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.identify(a, device_id=DEVICE_B)  # attempted hijack
        await hub.broadcast("alert", {"body": "for-B"}, device_id=DEVICE_B)
        return a, hub.identity_of(a)

    a, ident = asyncio.run(run())
    assert ident["device_id"] == DEVICE_A
    assert a.sent == []  # never receives B's advisory


def test_unidentified_driver_socket_fails_closed():
    """A DRIVER-role socket with no device bound gets no addressed frames."""
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a = _FakeSocket("anon-driver")
        await hub.connect(a, role="DRIVER")  # token carried no device_id claim
        await hub.broadcast("alert", {"body": "for-A"}, device_id=DEVICE_A)
        return a

    assert asyncio.run(run()).sent == []


def test_disconnect_clears_identity():
    from gateway.ws import WsHub

    async def run():
        hub = WsHub()
        a = _FakeSocket("A")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        assert hub.driver_count == 1
        await hub.disconnect(a)
        return hub

    hub = asyncio.run(run())
    assert hub.client_count == 0 and hub.driver_count == 0
    assert hub.identity_of(_FakeSocket("gone")) == {}


# -------------------------------------------------------------- dispatcher
def test_dispatch_addresses_the_ws_leg_and_stamps_audience(monkeypatch):
    """dispatch() must not fan a driver advisory out to every socket."""
    from gateway import notifications
    from gateway.routers import push
    from gateway.ws import WsHub

    async def noop(gw, device_id, payload):
        return False

    monkeypatch.setattr(push, "deliver", noop)
    monkeypatch.setattr(push, "deliver_fcm", noop)

    async def run():
        hub = WsHub()
        a, b = _FakeSocket("A"), _FakeSocket("B")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.connect(b, device_id=DEVICE_B, role="DRIVER")
        await notifications.dispatch_alert(
            _FakeGw(hub), DEVICE_A, kind="WRONG_DIRECTION",
            title="Wrong-way driving", body="Correct your direction.",
        )
        return a, b

    a, b = asyncio.run(run())
    assert len(a.sent) == 1
    payload = a.sent[0]["payload"]
    assert payload["audience"] == "driver"      # explicitly targeted
    assert payload["device_id"] == DEVICE_A
    assert b.sent == []                          # driver B is not notified


def test_broadcast_audience_reaches_every_driver(monkeypatch):
    """audience='broadcast' keeps the all-driver fan-out (e.g. congestion)."""
    from gateway import notifications
    from gateway.routers import push
    from gateway.ws import WsHub

    async def noop(gw, device_id, payload):
        return False

    monkeypatch.setattr(push, "deliver", noop)
    monkeypatch.setattr(push, "deliver_fcm", noop)

    async def run():
        hub = WsHub()
        a, b = _FakeSocket("A"), _FakeSocket("B")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.connect(b, device_id=DEVICE_B, role="DRIVER")
        await notifications.dispatch_alert(
            _FakeGw(hub), DEVICE_A, kind="TRAFFIC_CONGESTION",
            title="Congestion", body="Expect delay.",
            audience=notifications.AUDIENCE_BROADCAST,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.sent and b.sent
    assert a.sent[0]["payload"]["audience"] == "broadcast"


def test_extra_cannot_widen_or_repoint_an_advisory(monkeypatch):
    """A caller's `extra` must not override the resolved target/audience."""
    from gateway import notifications
    from gateway.routers import push
    from gateway.ws import WsHub

    async def noop(gw, device_id, payload):
        return False

    monkeypatch.setattr(push, "deliver", noop)
    monkeypatch.setattr(push, "deliver_fcm", noop)

    async def run():
        hub = WsHub()
        a, b = _FakeSocket("A"), _FakeSocket("B")
        await hub.connect(a, device_id=DEVICE_A, role="DRIVER")
        await hub.connect(b, device_id=DEVICE_B, role="DRIVER")
        await notifications.dispatch_alert(
            _FakeGw(hub), DEVICE_A, kind="accident", title="t", body="b",
            extra={"device_id": DEVICE_B, "audience": "broadcast"},
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.sent[0]["payload"]["device_id"] == DEVICE_A
    assert a.sent[0]["payload"]["audience"] == "driver"
    assert b.sent == []


# -------------------------------------------------------------- end-to-end
@pytest.fixture()
def client():
    from gateway.main import app

    with TestClient(app) as c:
        yield c


def _event(client, device_id: str, marker: str):
    r = client.post("/api/ai/event", json={
        "event_type": "WRONG_DIRECTION",
        "device_id": device_id,
        "payload": {"message": marker},
        "severity": "critical",
    })
    assert r.status_code == 200, r.text
    return r.json()


def test_e2e_driver_a_receives_driver_b_does_not(client):
    """Two live sockets, the real /api/ai/event path: A is notified, B is not.

    Non-delivery is proven by interleaving: A-1, B-1, A-2. Reading two frames off
    A must yield A-1 then A-2. If B's advisory had leaked into A's stream the
    second frame would be B-1 and this fails.
    """
    with client.websocket_connect(f"/api/ws?device={DEVICE_A}") as ws_a, \
         client.websocket_connect(f"/api/ws?device={DEVICE_B}") as ws_b:
        assert ws_a.receive_json()["type"] == "hello"
        assert ws_b.receive_json()["type"] == "hello"

        _event(client, DEVICE_A, "A-1")
        _event(client, DEVICE_B, "B-1")
        _event(client, DEVICE_A, "A-2")

        a_first = ws_a.receive_json()
        a_second = ws_a.receive_json()
        b_first = ws_b.receive_json()

    # Driver A got exactly its own two advisories, in order.
    assert [f["payload"]["body"] for f in (a_first, a_second)] == ["A-1", "A-2"]
    assert all(f["payload"]["device_id"] == DEVICE_A for f in (a_first, a_second))
    # Driver B's first frame is its OWN advisory — A-1 never reached it.
    assert b_first["payload"]["body"] == "B-1"
    assert b_first["payload"]["device_id"] == DEVICE_B


def test_e2e_dashboard_still_sees_every_driver_advisory(client):
    """The control room is not driver-scoped and must keep the full picture."""
    with client.websocket_connect("/api/ws") as dash, \
         client.websocket_connect(f"/api/ws?device={DEVICE_A}") as ws_a:
        assert dash.receive_json()["type"] == "hello"
        assert ws_a.receive_json()["type"] == "hello"

        _event(client, DEVICE_A, "A-1")
        _event(client, DEVICE_B, "B-1")

        seen = [dash.receive_json()["payload"]["body"] for _ in range(2)]

    assert seen == ["A-1", "B-1"]


def test_e2e_identify_frame_binds_the_socket(client):
    """The PWA's identify frame isolates a socket opened without ?device=."""
    with client.websocket_connect("/api/ws") as ws_a, \
         client.websocket_connect(f"/api/ws?device={DEVICE_B}") as ws_b:
        assert ws_a.receive_json()["type"] == "hello"
        assert ws_b.receive_json()["type"] == "hello"

        ws_a.send_text('{"cmd":"identify","device_id":"%s"}' % DEVICE_A)
        # Round-trip a ping so the identify is processed before the events fire.
        _event(client, DEVICE_A, "A-1")
        _event(client, DEVICE_B, "B-1")

        a_first = ws_a.receive_json()
        b_first = ws_b.receive_json()

    assert a_first["payload"]["body"] == "A-1"
    assert b_first["payload"]["body"] == "B-1"
