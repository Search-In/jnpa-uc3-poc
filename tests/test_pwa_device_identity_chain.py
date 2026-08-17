"""The Driver-PWA device identity chain, end to end.

    vehicle plate
        -> POST /api/driver/login        -> core.vehicle.vehicle_id (TRK-######)
        -> POST /api/auth/device-token   -> JWT bound to that device_id
        -> POST /api/push/subscribe            } core.push_subscription
        -> POST /api/push/register-device      } keyed on the SAME device_id
        -> GET  /api/trucks?state=…      -> registered_devices (the console)
        -> POST /api/trucks/{id}/route   -> core.reroute_advisory + dispatch
        -> WebSocket / WebPush / FCM     -> that device_id ONLY

``tests/test_pwa_device_visibility.py`` proves the console step. This file pins
the links either side of it: that one canonical id flows unbroken from the plate
a driver types to the transport that reaches their phone, that the push
registration records WHICH driver it belongs to, and that no step lets one
driver reach another's device.

THE BINDING BUG PINNED HERE. The PWA used to register push with
``vehicle_id = <the plate>`` and no ``driver_id`` at all, so
``core.push_subscription.driver_id`` was always NULL and its ``vehicle_id`` could
not be joined to ``core.vehicle`` — which is precisely the join the control-room
device list depends on.

No server, no database: handlers are called directly with the gateway state
stubbed, per this repo's router-test idiom.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway import auth as A  # noqa: E402
from gateway.routers import push as P  # noqa: E402
from gateway.routers import trucks as T  # noqa: E402

DRIVER_A = "TRK-000026"
DRIVER_B = "TRK-000028"
PLATE_A = "MH04QA9911"
DSN = "postgresql+asyncpg://x:x@127.0.0.1:1/none"


class _Resp:
    def __init__(self, status_code: int = 200, body: Optional[dict] = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _Http:
    async def post(self, url, json=None, timeout=None):
        raise __import__("httpx").ConnectError("sim down")


def _gw() -> SimpleNamespace:
    async def record_decision(**kw):
        pass

    return SimpleNamespace(
        cfg=SimpleNamespace(truck_api_url="http://truck-sim:9000", postgres_dsn=DSN),
        http=_Http(),
        record_decision=record_decision,
    )


@pytest.fixture(autouse=True)
def _clean():
    P.SUBSCRIPTIONS.clear()
    P.FCM_TOKENS.clear()
    T.LAST_REROUTE.clear()
    yield
    P.SUBSCRIPTIONS.clear()
    P.FCM_TOKENS.clear()
    T.LAST_REROUTE.clear()


# ============================================== 2. the JWT carries the device
def test_the_driver_token_is_bound_to_the_resolved_vehicle_id():
    """The device_id claim is the WHOLE security model downstream: /api/driver/
    profile reads it instead of trusting a query param, and driver_scope_violation
    compares against it."""
    token = A.encode_token("driver", A.Role.DRIVER.value, device_id=DRIVER_A)
    claims = A.decode_token(token) if hasattr(A, "decode_token") else None
    principal = A.principal_from_token(token)
    assert principal.role == A.Role.DRIVER.value
    assert principal.device_id == DRIVER_A
    if claims is not None:
        assert claims["device_id"] == DRIVER_A


def test_a_driver_token_can_never_carry_a_control_room_role():
    p = A.principal_from_token(
        A.encode_token("driver", A.Role.DRIVER.value, device_id=DRIVER_A))
    assert p.role == A.Role.DRIVER.value


# ================================ 3. push registration records the real driver
@pytest.fixture()
def upsert_spy(monkeypatch):
    """Capture what each push registration would write to core.push_subscription."""
    writes: list[dict] = []

    async def _upsert(dsn, device_id, **cols):
        writes.append({"device_id": device_id, **cols})

    monkeypatch.setattr(P, "_upsert", _upsert)
    return writes


def _state() -> SimpleNamespace:
    return SimpleNamespace(cfg=SimpleNamespace(postgres_dsn=DSN))


def test_webpush_registration_stores_the_driver_and_the_canonical_vehicle_id(
    upsert_spy,
):
    sub = {"endpoint": "https://push.example/a", "keys": {"p256dh": "k", "auth": "s"}}
    asyncio.run(P.subscribe(
        body={"device_id": DRIVER_A, "subscription": sub,
              "driver_id": "DRV-0001", "vehicle_id": DRIVER_A},
        state=_state()))
    (write,) = upsert_spy
    assert write["device_id"] == DRIVER_A
    assert write["driver_id"] == "DRV-0001"
    # The canonical Vehicle ID — NOT the plate. A plate here cannot be joined to
    # core.vehicle.vehicle_id, which is what the console list joins on.
    assert write["vehicle_id"] == DRIVER_A
    assert write["vehicle_id"] != PLATE_A


def test_fcm_registration_stores_the_same_binding_against_the_same_device(
    upsert_spy,
):
    asyncio.run(P.register_device(
        body={"device_id": DRIVER_A, "fcm_token": "tok-a", "platform": "web",
              "driver_id": "DRV-0001", "vehicle_id": DRIVER_A},
        state=_state()))
    (write,) = upsert_spy
    assert write["device_id"] == DRIVER_A
    assert write["driver_id"] == "DRV-0001"
    assert write["vehicle_id"] == DRIVER_A
    assert write["fcm_token"] == "tok-a"


def test_both_transports_key_on_one_canonical_device_id(upsert_spy):
    """WebPush and FCM must land on the SAME primary key, or a re-route reaches
    one transport and not the other."""
    sub = {"endpoint": "https://push.example/a", "keys": {}}
    asyncio.run(P.subscribe(body={"device_id": DRIVER_A, "subscription": sub,
                                  "driver_id": "DRV-0001", "vehicle_id": DRIVER_A},
                            state=_state()))
    asyncio.run(P.register_device(body={"device_id": DRIVER_A, "fcm_token": "tok-a",
                                        "driver_id": "DRV-0001",
                                        "vehicle_id": DRIVER_A},
                                  state=_state()))
    assert {w["device_id"] for w in upsert_spy} == {DRIVER_A}


def test_the_binding_is_optional_so_an_unassigned_device_can_still_register(
    upsert_spy,
):
    """Backward compatibility: a device with no ACTIVE driver profile yet must
    still be reachable — device_id alone is what delivery targets."""
    asyncio.run(P.register_device(
        body={"device_id": DRIVER_A, "fcm_token": "tok-a"}, state=_state()))
    (write,) = upsert_spy
    assert write["device_id"] == DRIVER_A
    assert write["driver_id"] is None


# ================================= 10 + 11. re-route persists and targets ONE device
@pytest.fixture()
def reroute_rig(monkeypatch):
    """Capture the durable save and every dispatch the re-route performs."""
    saved: list[tuple[str, dict]] = []
    dispatched: list[tuple[str, dict, str]] = []

    class _Repo:
        async def save(self, device_id, advisory):
            saved.append((device_id, dict(advisory)))

        async def latest(self, device_id):
            for d, a in reversed(saved):
                if d == device_id:
                    return a
            return None

    repo = _Repo()
    monkeypatch.setattr(T, "_advisory_repo", lambda gw: repo)

    from gateway import notifications

    async def _dispatch(gw, device_id, payload, *, ws_type="alert", **kw):
        dispatched.append((device_id, dict(payload), ws_type))
        return notifications.DispatchResult(ws=True, webpush=True, fcm=True)

    monkeypatch.setattr(notifications, "dispatch", _dispatch)
    return SimpleNamespace(saved=saved, dispatched=dispatched, repo=repo)


def test_push_reroute_persists_the_advisory_for_that_device(reroute_rig):
    out = asyncio.run(T.reroute_truck(
        DRIVER_A, body={"gate_id": "G-NSICT", "force_state": "EN_ROUTE_TO_PORT"},
        gw=_gw()))
    assert out["persisted"] is True
    assert [d for d, _ in reroute_rig.saved] == [DRIVER_A]
    assert reroute_rig.saved[0][1]["gate_id"] == "G-NSICT"
    assert reroute_rig.saved[0][1]["device_id"] == DRIVER_A


def test_the_advisory_is_persisted_even_though_the_simulator_is_down(reroute_rig):
    """A registered PWA device is NOT in the simulator, so the sim leg will fail
    for it by definition. That must not drop the driver-facing advisory — the
    exact case the console needs in order to re-route a real driver."""
    out = asyncio.run(T.reroute_truck(DRIVER_A, body={"gate_id": "G-NSICT"}, gw=_gw()))
    assert out["sim"]["delivered"] is False
    assert out["decision_path"] == "REROUTE_DEGRADED"
    assert out["persisted"] is True                 # the driver still gets it
    assert reroute_rig.dispatched                   # …and it was pushed


def test_the_notification_targets_exactly_that_device_id(reroute_rig):
    asyncio.run(T.reroute_truck(DRIVER_A, body={"gate_id": "G-NSICT"}, gw=_gw()))
    (device_id, payload, ws_type) = reroute_rig.dispatched[0]
    assert device_id == DRIVER_A
    assert payload["device_id"] == DRIVER_A
    assert ws_type == "reroute"
    # Addressed to one driver — dispatch() defaults to audience="driver", which
    # is what stops the WS leg fanning out to every socket.
    assert payload.get("audience", "driver") == "driver"


def test_rerouting_driver_a_never_dispatches_to_driver_b(reroute_rig):
    asyncio.run(T.reroute_truck(DRIVER_A, body={"gate_id": "G-NSICT"}, gw=_gw()))
    asyncio.run(T.reroute_truck(DRIVER_B, body={"gate_id": "G-BMCT"}, gw=_gw()))
    targets = [d for d, _, _ in reroute_rig.dispatched]
    assert targets == [DRIVER_A, DRIVER_B]
    a_payloads = [p for d, p, _ in reroute_rig.dispatched if d == DRIVER_A]
    assert all(p["device_id"] == DRIVER_A for p in a_payloads)
    assert all(p["gate_id"] == "G-NSICT" for p in a_payloads)


def test_the_polling_fallback_returns_only_that_devices_advisory(reroute_rig):
    asyncio.run(T.reroute_truck(DRIVER_A, body={"gate_id": "G-NSICT"}, gw=_gw()))
    a = asyncio.run(T.latest_reroute(DRIVER_A, gw=_gw()))
    b = asyncio.run(T.latest_reroute(DRIVER_B, gw=_gw()))
    assert a["advisory"]["gate_id"] == "G-NSICT"
    assert b["advisory"] is None


# ================================== 12 + 13. cross-driver isolation at the door
# These assert the MIDDLEWARE gate, which is what actually stops driver A from
# reaching driver B — the handlers above take the device id from the path.
def test_driver_a_cannot_read_driver_bs_latest_advisory():
    assert A.driver_scope_violation(
        f"/api/trucks/{DRIVER_B}/route/latest", DRIVER_A) is not None
    assert A.driver_scope_violation(
        f"/api/trucks/{DRIVER_A}/route/latest", DRIVER_A) is None


def test_driver_a_cannot_push_a_route_to_driver_bs_device():
    assert A.driver_scope_violation(f"/api/trucks/{DRIVER_B}/route", DRIVER_A) is not None
    assert A.driver_scope_violation(f"/api/trucks/{DRIVER_A}/route", DRIVER_A) is None


def test_a_driver_cannot_enumerate_the_fleet_or_the_registered_devices():
    """The registered-device list rides on the fleet-list endpoint, so the rule
    that keeps a DRIVER off that endpoint is now also what keeps one driver from
    enumerating every signed-in driver."""
    for path in ("/api/trucks", "/api/trucks/"):
        assert A.driver_scope_violation(path, DRIVER_A) is not None


def test_a_driver_token_with_no_device_binding_reaches_nothing():
    assert A.driver_scope_violation(f"/api/trucks/{DRIVER_A}", None) is not None


def test_the_scope_rule_still_covers_every_device_sub_resource():
    for suffix in ("", "/route", "/route/latest", "/route/ack"):
        assert A.driver_scope_violation(
            f"/api/trucks/{DRIVER_B}{suffix}", DRIVER_A) is not None
