"""UC-II -> UC-III cross-twin: publish -> consume -> persist -> notify.

The audit found XT-2 (``jnpa.crosstwin.deferred-arrival``) half-wired: the
consumer, the TAS metering and the UI panel existed, but

  * the applied window lived ONLY in ``gateway.tas_mock._WINDOWS`` (in-memory,
    cap 32), so it vanished on restart — "loaded data disappears after refresh";
  * there was NO producer anywhere in the repository, so the flow could not be
    fired at all without the separate UC-II stack publishing on Kafka;
  * no driver was ever notified, although metering arrivals is the entire point.

These tests pin the closed loop through ``gateway.crosstwin.apply``, the single
path both transports now take.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from gateway import crosstwin, tas_mock
from jnpa_shared.schemas import DeferredArrivalWindow


class FakeWs:
    def __init__(self) -> None:
        self.frames: list[tuple[str, Any, Optional[str]]] = []

    async def broadcast(self, type_, payload, *, device_id=None):
        self.frames.append((type_, payload, device_id))


class FakeCfg:
    postgres_dsn = "postgresql://fake/never-connected"


class FakeGw:
    def __init__(self) -> None:
        self.ws = FakeWs()
        self.cfg = FakeCfg()


class FakeDeferredRepo:
    """Stands in for core.deferred_arrival_window."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.bumps: list[str] = []

    async def upsert(self, window, *, transport="KAFKA") -> bool:
        cid = window["correlation_id"]
        prev = self.rows.get(cid, {})
        merged = {**window, "transport": transport,
                  "booked": max(int(prev.get("booked", 0)),
                                int(window.get("booked", 0)))}
        self.rows[cid] = merged
        return True

    async def bump_booked(self, correlation_id: str) -> None:
        self.bumps.append(correlation_id)
        if correlation_id in self.rows:
            self.rows[correlation_id]["booked"] += 1

    async def recent(self, limit: int = 32) -> list[dict]:
        return list(self.rows.values())[:limit]


def _window(**kw) -> DeferredArrivalWindow:
    base = {
        "correlation_id": "S2-20260615",
        "gate_id": "G-NSICT",
        "window_start": datetime.now(timezone.utc).replace(microsecond=0),
        "window_min": 90,
        "slot_cap": 4,
        "source": "UC-II",
    }
    base.update(kw)
    return DeferredArrivalWindow(**base)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Isolate the module-level slot book and repo between tests."""
    tas_mock._WINDOWS.clear()
    crosstwin.reset_for_tests()
    repo = FakeDeferredRepo()
    monkeypatch.setattr(crosstwin, "_repo", lambda gw: repo)
    yield repo
    tas_mock._WINDOWS.clear()
    crosstwin.reset_for_tests()


@pytest.fixture
def no_drivers(monkeypatch):
    """Default: no TAS bookings, so the notify leg is a clean no-op."""
    async def _none(gw, gate_id, slot_codes):
        return []

    monkeypatch.setattr(crosstwin, "_affected_devices", _none)


# --------------------------------------------------------- consume + persist
@pytest.mark.asyncio
async def test_consumed_window_is_persisted_and_announced(clean, no_drivers):
    gw = FakeGw()
    res = await crosstwin.apply(gw, _window(), transport="KAFKA")

    assert res["persisted"] is True
    # Persisted with the contract's correlation id — the idempotency key.
    assert "S2-20260615" in clean.rows
    assert clean.rows["S2-20260615"]["slot_cap"] == 4
    assert clean.rows["S2-20260615"]["transport"] == "KAFKA"

    # The control room is told; the frame is unaddressed (dashboard-wide) and the
    # PWA ignores type=tas, so no driver sees another driver's slot data.
    kinds = [(t, d) for t, _p, d in gw.ws.frames]
    assert ("tas", None) in kinds


@pytest.mark.asyncio
async def test_redelivery_is_idempotent(clean, no_drivers):
    """UC-II may redeliver after a rebalance; the window must not double-count."""
    gw = FakeGw()
    await crosstwin.apply(gw, _window(), transport="KAFKA")
    await crosstwin.apply(gw, _window(), transport="KAFKA")

    assert len(clean.rows) == 1
    assert len(tas_mock.deferred_windows()) == 1


@pytest.mark.asyncio
async def test_http_transport_takes_the_same_path(clean, no_drivers):
    """The HTTP inject exists so XT-2 is demoable without the UC-II producer."""
    gw = FakeGw()
    res = await crosstwin.apply(gw, _window(correlation_id="HTTP-1"), transport="HTTP")

    assert res["transport"] == "HTTP"
    assert clean.rows["HTTP-1"]["transport"] == "HTTP"
    # Same metering effect as the Kafka path.
    assert any(w["correlation_id"] == "HTTP-1" for w in tas_mock.deferred_windows())


# ------------------------------------------------------------- slot metering
@pytest.mark.asyncio
async def test_window_caps_bookings_inside_it(clean, no_drivers):
    gw = FakeGw()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    await crosstwin.apply(gw, _window(window_start=start, slot_cap=2), transport="HTTP")

    inside = start + timedelta(minutes=10)
    assert tas_mock.check_booking_allowed("G-NSICT", inside)[0] is True
    assert tas_mock.check_booking_allowed("G-NSICT", inside)[0] is True
    allowed, refusing = tas_mock.check_booking_allowed("G-NSICT", inside)
    assert allowed is False
    assert refusing["correlation_id"] == "S2-20260615"     # the demo's "WHY"

    # Outside the window the cap does not apply.
    outside = start + timedelta(minutes=200)
    assert tas_mock.check_booking_allowed("G-NSICT", outside)[0] is True


@pytest.mark.asyncio
async def test_other_gates_are_unaffected(clean, no_drivers):
    gw = FakeGw()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    await crosstwin.apply(gw, _window(window_start=start, gate_id="G-NSICT", slot_cap=0),
                          transport="HTTP")
    inside = start + timedelta(minutes=5)
    assert tas_mock.check_booking_allowed("G-NSICT", inside)[0] is False
    assert tas_mock.check_booking_allowed("G-JNPCT", inside)[0] is True


# ----------------------------------------------------------------- restart
@pytest.mark.asyncio
async def test_window_survives_a_restart(clean, no_drivers):
    """The "loaded data disappears after refresh" report, cross-twin edition."""
    gw = FakeGw()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    await crosstwin.apply(gw, _window(window_start=start, slot_cap=1), transport="KAFKA")

    tas_mock._WINDOWS.clear()                      # simulate a gateway restart
    assert tas_mock.deferred_windows() == []

    restored = await crosstwin.restore(gw)
    assert restored == 1
    windows = tas_mock.deferred_windows()
    assert len(windows) == 1
    assert windows[0]["correlation_id"] == "S2-20260615"
    # The metering CAP is what had to survive, and it did.
    assert tas_mock.check_booking_allowed("G-NSICT", start + timedelta(minutes=5))[0] is True
    assert tas_mock.check_booking_allowed("G-NSICT", start + timedelta(minutes=5))[0] is False


@pytest.mark.asyncio
async def test_restore_preserves_the_booked_counter(clean, no_drivers):
    """A restart must not hand back capacity that live bookings already consumed."""
    gw = FakeGw()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    await crosstwin.apply(gw, _window(window_start=start, slot_cap=2), transport="KAFKA")
    tas_mock.check_booking_allowed("G-NSICT", start + timedelta(minutes=5))
    await clean.bump_booked("S2-20260615")         # what /api/tas/book mirrors

    tas_mock._WINDOWS.clear()
    await crosstwin.restore(gw)

    # One of the two slots was already taken, so only one remains.
    assert tas_mock.check_booking_allowed("G-NSICT", start + timedelta(minutes=5))[0] is True
    assert tas_mock.check_booking_allowed("G-NSICT", start + timedelta(minutes=5))[0] is False


# ------------------------------------------------------------- driver notify
@pytest.mark.asyncio
async def test_affected_drivers_are_notified_on_their_own_device_only(clean, monkeypatch):
    """The contract meters arrivals — the drivers whose slots moved must be told,
    and told individually."""
    gw = FakeGw()

    async def _affected(gw_, gate_id, slot_codes):
        return [{"vehicle_id": "TRK-000001", "driver_id": "DRV-1",
                 "slot_code": "G-NSICT-2026-06-15-1300"},
                {"vehicle_id": "TRK-000002", "driver_id": "DRV-2",
                 "slot_code": "G-NSICT-2026-06-15-1300"}]

    monkeypatch.setattr(crosstwin, "_affected_devices", _affected)

    dispatched: list[tuple[str, str]] = []

    async def _fake_resolve(state, *, driver_id=None, vehicle_id=None):
        return {"DRV-1": "TRK-000001", "DRV-2": "TRK-000002"}.get(driver_id)

    async def _fake_dispatch_alert(state, device_id, **kw):
        dispatched.append((device_id, kw["kind"]))
        return object()

    import gateway.notifications as N
    import gateway.routers.push as P

    monkeypatch.setattr(P, "resolve_device", _fake_resolve)
    monkeypatch.setattr(N, "dispatch_alert", _fake_dispatch_alert)

    res = await crosstwin.apply(gw, _window(), transport="HTTP")

    assert res["notified"] == 2
    assert dispatched == [("TRK-000001", "TAS_RESLOT"), ("TRK-000002", "TAS_RESLOT")]
    # dispatch_alert addresses by device_id, so driver A's re-slot never reaches B.
    assert len({d for d, _ in dispatched}) == 2


@pytest.mark.asyncio
async def test_a_failing_push_never_blocks_metering(clean, monkeypatch):
    """Metering is the contract; a push outage must not undo it."""
    gw = FakeGw()

    async def _boom(gw_, gate_id, slot_codes):
        raise RuntimeError("push subsystem down")

    monkeypatch.setattr(crosstwin, "_affected_devices", _boom)

    res = await crosstwin.apply(gw, _window(), transport="KAFKA")
    assert res["notified"] == 0
    assert res["persisted"] is True
    assert len(tas_mock.deferred_windows()) == 1     # the window still applied


def test_repository_coerces_iso_timestamps_for_asyncpg():
    """tas_mock hands over ISO STRINGS; asyncpg binds by Python type.

    Caught by the live-RDS smoke, not by a fake repo: asyncpg rejects a str for a
    timestamptz column ("expected a datetime.date or datetime.datetime instance")
    and a CAST in the SQL does not help, because the driver never gets that far.
    Every persisted window was silently lost to the best-effort except.
    """
    from datetime import datetime as _dt

    from services.crosstwin.repository import _as_dt

    coerced = _as_dt("2026-08-03T12:54:07+00:00")
    assert isinstance(coerced, _dt) and coerced.tzinfo is not None

    # Naive input is treated as UTC rather than handed over ambiguously.
    naive = _as_dt("2026-08-03T12:54:07")
    assert naive.tzinfo is not None

    # A datetime passes through untouched, and None stays None.
    now = _dt.now(timezone.utc)
    assert _as_dt(now) is now
    assert _as_dt(None) is None


@pytest.mark.asyncio
async def test_window_dict_shape_is_persistable():
    """The exact dict tas_mock produces must be accepted by the repository."""
    from services.crosstwin.repository import _as_dt

    tas_mock._WINDOWS.clear()
    tas_mock.apply_deferred_window(_window(correlation_id="SHAPE-1"))
    w = tas_mock.deferred_windows()[0]
    # These are the two fields that broke against real Postgres.
    assert isinstance(w["window_start"], str)
    assert _as_dt(w["window_start"]) is not None
    assert _as_dt(w["window_end"]) is not None
    tas_mock._WINDOWS.clear()


@pytest.mark.asyncio
async def test_a_failing_database_never_blocks_metering(clean, no_drivers, monkeypatch):
    gw = FakeGw()

    class DeadRepo:
        async def upsert(self, window, *, transport="KAFKA"):
            return False

    monkeypatch.setattr(crosstwin, "_repo", lambda gw_: DeadRepo())
    res = await crosstwin.apply(gw, _window(), transport="KAFKA")

    assert res["persisted"] is False
    assert len(tas_mock.deferred_windows()) == 1     # metering happened anyway
