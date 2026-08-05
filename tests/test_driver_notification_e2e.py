"""Driver notification isolation, END TO END across ALL THREE transports.

``tests/test_notification_isolation.py`` proves the WebSocket leg is addressed.
It does NOT prove the push legs are: both `push.deliver` and `push.deliver_fcm`
are stubbed to no-ops there, so a bug that fanned WebPush or FCM out to every
registered device would pass that suite unnoticed. WS isolation with a leaking
push transport is not isolation.

This suite closes that gap for the two drivers named in the demo script:

    Driver A = TRK-000026        Driver B = TRK-000028

Asserted, for every transport:

  1. WebSocket   — A's advisory reaches A's socket only.
  2. WebPush     — only A's stored subscription is written to.
  3. FCM         — only A's device token is written to.
  4. Dashboard   — the control-room socket still sees everything.
  5. Binding     — an unbound or wrongly-bound socket receives nothing.

The push transports are captured (not stubbed away), so a regression that
broadened their addressing fails here.
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

DRIVER_A = "TRK-000026"
DRIVER_B = "TRK-000028"


# --------------------------------------------------------------------- fakes
class _Socket:
    """Stand-in for a starlette WebSocket (accept + send_json)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[dict] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, message) -> None:
        self.sent.append(message)

    def bodies(self) -> list:
        return [m["payload"].get("body") for m in self.sent]

    def targets(self) -> list:
        return [m["payload"].get("device_id") for m in self.sent]


class _Gw:
    def __init__(self, hub) -> None:
        self.ws = hub


class _PushSpy:
    """Records every (device_id, payload) each push transport was asked to send."""

    def __init__(self) -> None:
        self.webpush: list[tuple[str, dict]] = []
        self.fcm: list[tuple[str, dict]] = []

    def install(self, monkeypatch, *, registered: set[str]) -> None:
        from gateway.routers import push

        async def _deliver(gw, device_id, payload):
            self.webpush.append((device_id, payload))
            return device_id in registered

        async def _deliver_fcm(gw, device_id, payload):
            self.fcm.append((device_id, payload))
            return device_id in registered

        monkeypatch.setattr(push, "deliver", _deliver)
        monkeypatch.setattr(push, "deliver_fcm", _deliver_fcm)

    def webpush_devices(self) -> set[str]:
        return {d for d, _ in self.webpush}

    def fcm_devices(self) -> set[str]:
        return {d for d, _ in self.fcm}


async def _hub_with_both_drivers():
    """A hub holding driver A, driver B and one control-room socket."""
    from gateway.ws import WsHub

    hub = WsHub()
    a, b, dash = _Socket("A"), _Socket("B"), _Socket("dash")
    await hub.connect(a, device_id=DRIVER_A, role="DRIVER")
    await hub.connect(b, device_id=DRIVER_B, role="DRIVER")
    await hub.connect(dash)  # control room: no device binding
    return hub, a, b, dash


# ============================================ 1-4. all transports at once
def test_advisory_for_A_reaches_only_A_on_every_transport(monkeypatch):
    """The headline case. WS, WebPush and FCM must all address A alone."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered={DRIVER_A, DRIVER_B})

    async def run():
        hub, a, b, dash = await _hub_with_both_drivers()
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="REROUTE",
            title="Route changed", body="Use Gate 3 via the service road.",
            category="route",
        )
        return a, b, dash

    a, b, dash = asyncio.run(run())

    # 1. WebSocket
    assert a.bodies() == ["Use Gate 3 via the service road."]
    assert b.sent == [], "driver B received driver A's advisory over WebSocket"

    # 2. WebPush — B's subscription must never be touched
    assert spy.webpush_devices() == {DRIVER_A}, (
        f"WebPush addressed {spy.webpush_devices()}, expected only {DRIVER_A}")

    # 3. FCM
    assert spy.fcm_devices() == {DRIVER_A}, (
        f"FCM addressed {spy.fcm_devices()}, expected only {DRIVER_A}")

    # 4. Control room still sees it
    assert dash.bodies() == ["Use Gate 3 via the service road."]
    assert dash.targets() == [DRIVER_A]


def test_the_reverse_direction_holds_too(monkeypatch):
    """Symmetry: an advisory for B must not reach A. Not implied by the above —
    a hard-coded 'first driver wins' bug would pass the first test."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered={DRIVER_A, DRIVER_B})

    async def run():
        hub, a, b, _ = await _hub_with_both_drivers()
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_B, kind="SOS_ACK", title="SOS received",
            body="Help is on the way.",
        )
        return a, b

    a, b = asyncio.run(run())
    assert b.bodies() == ["Help is on the way."]
    assert a.sent == []
    assert spy.webpush_devices() == {DRIVER_B}
    assert spy.fcm_devices() == {DRIVER_B}


def test_interleaved_advisories_stay_in_their_own_lanes(monkeypatch):
    """Three dispatches, alternating targets — proves NON-delivery without
    relying on a timeout: each socket's stream must contain only its own."""
    from gateway import notifications

    _PushSpy().install(monkeypatch, registered=set())

    async def run():
        hub, a, b, dash = await _hub_with_both_drivers()
        gw = _Gw(hub)
        await notifications.dispatch_alert(gw, DRIVER_A, kind="k", title="t", body="A-1")
        await notifications.dispatch_alert(gw, DRIVER_B, kind="k", title="t", body="B-1")
        await notifications.dispatch_alert(gw, DRIVER_A, kind="k", title="t", body="A-2")
        return a, b, dash

    a, b, dash = asyncio.run(run())
    assert a.bodies() == ["A-1", "A-2"]
    assert b.bodies() == ["B-1"]
    # The control room is the ONE place that legitimately sees all three.
    assert dash.bodies() == ["A-1", "B-1", "A-2"]


# ================================================= 5. binding failure modes
def test_unbound_driver_socket_receives_nothing(monkeypatch):
    """A DRIVER socket whose token carried no device_id fails CLOSED."""
    from gateway import notifications
    from gateway.ws import WsHub

    _PushSpy().install(monkeypatch, registered=set())

    async def run():
        hub = WsHub()
        anon = _Socket("unbound")
        await hub.connect(anon, role="DRIVER")   # no device_id claim
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="k", title="t", body="for-A")
        return anon

    assert asyncio.run(run()).sent == []


def test_identify_binds_an_anonymous_socket_then_isolates_it(monkeypatch):
    """AUTH_ENABLED=false path: the PWA's identify frame is what makes the
    demo profile isolate correctly."""
    from gateway import notifications
    from gateway.ws import WsHub

    _PushSpy().install(monkeypatch, registered=set())

    async def run():
        hub = WsHub()
        a, b = _Socket("A"), _Socket("B")
        await hub.connect(a)                      # anonymous
        await hub.connect(b)
        await hub.identify(a, device_id=DRIVER_A)
        await hub.identify(b, device_id=DRIVER_B)
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="k", title="t", body="for-A")
        return a, b

    a, b = asyncio.run(run())
    assert a.bodies() == ["for-A"]
    assert b.sent == []


def test_a_socket_cannot_repoint_itself_at_another_driver(monkeypatch):
    """A verified JWT binding wins over a client-supplied identify frame —
    otherwise any paired device could subscribe to another driver's advisories."""
    from gateway import notifications
    from gateway.ws import WsHub

    _PushSpy().install(monkeypatch, registered=set())

    async def run():
        hub = WsHub()
        attacker = _Socket("B-pretending-to-be-A")
        await hub.connect(attacker, device_id=DRIVER_B, role="DRIVER")
        await hub.identify(attacker, device_id=DRIVER_A)     # attempted hijack
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="k", title="t", body="for-A")
        return attacker, hub.identity_of(attacker)

    attacker, ident = asyncio.run(run())
    assert ident["device_id"] == DRIVER_B, "identify() overrode a verified binding"
    assert attacker.sent == []


# ======================================== push-subscription bookkeeping
def test_delivery_is_attempted_even_for_an_unregistered_device(monkeypatch):
    """A driver with no stored subscription must still be ATTEMPTED and reported
    as undelivered — silently skipping would hide a broken pairing."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered=set())     # nobody registered

    async def run():
        hub, a, _b, _d = await _hub_with_both_drivers()
        res = await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="k", title="t", body="for-A")
        return a, res

    a, res = asyncio.run(run())
    assert a.bodies() == ["for-A"]              # WS still delivered
    assert spy.webpush_devices() == {DRIVER_A}  # attempt was made
    assert res is not None


def test_unknown_device_is_a_no_op_not_a_broadcast(monkeypatch):
    """dispatch_alert(None) must not degrade into an all-driver fan-out."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered={DRIVER_A, DRIVER_B})

    async def run():
        hub, a, b, dash = await _hub_with_both_drivers()
        res = await notifications.dispatch_alert(
            _Gw(hub), None, kind="k", title="t", body="orphan")
        return res, a, b, dash

    res, a, b, dash = asyncio.run(run())
    assert res is None
    assert a.sent == [] and b.sent == [] and dash.sent == []
    assert spy.webpush == [] and spy.fcm == []


# ================================================ deliberate broadcasts
def test_broadcast_audience_still_reaches_every_driver(monkeypatch):
    """Isolation must not have broken the legitimate all-driver case
    (congestion, weather) — that would be a different regression."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered={DRIVER_A, DRIVER_B})

    async def run():
        hub, a, b, _ = await _hub_with_both_drivers()
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="TRAFFIC_CONGESTION",
            title="Congestion", body="Expect delay at Y-Junction.",
            audience=notifications.AUDIENCE_BROADCAST,
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.bodies() == b.bodies() == ["Expect delay at Y-Junction."]
    assert a.sent[0]["payload"]["audience"] == "broadcast"


def test_caller_extra_cannot_repoint_the_push_legs(monkeypatch):
    """`extra` is caller-supplied. It must not be able to redirect or widen an
    advisory — on ANY transport, not just WebSocket."""
    from gateway import notifications

    spy = _PushSpy()
    spy.install(monkeypatch, registered={DRIVER_A, DRIVER_B})

    async def run():
        hub, a, b, _ = await _hub_with_both_drivers()
        await notifications.dispatch_alert(
            _Gw(hub), DRIVER_A, kind="k", title="t", body="for-A",
            extra={"device_id": DRIVER_B, "audience": "broadcast"},
        )
        return a, b

    a, b = asyncio.run(run())
    assert a.sent[0]["payload"]["device_id"] == DRIVER_A
    assert a.sent[0]["payload"]["audience"] == "driver"
    assert b.sent == []
    assert spy.webpush_devices() == {DRIVER_A}
    assert spy.fcm_devices() == {DRIVER_A}
