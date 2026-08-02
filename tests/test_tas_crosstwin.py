"""Cross-twin XT-2: DeferredArrivalWindow -> TAS metering (gateway/tas_mock).

Unit layer (always runs): apply/window/cap logic on the in-memory slot book.
The live consumption path (Kafka topic -> gateway pump -> tas_mock) is asserted
by the e2e drill in docs/REMEDIATION_PLAN.md P1.4 (produce an event, poll
GET /api/tas/deferred-windows on the running gateway).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from jnpa_shared.schemas import DeferredArrivalWindow  # noqa: E402

from gateway import tas_mock  # noqa: E402


def _win(**kw) -> DeferredArrivalWindow:
    base = dict(correlation_id="S2-TEST", window_start=datetime.now(tz=timezone.utc),
                window_min=90, slot_cap=2, gate_id="G-TEST")
    base.update(kw)
    return DeferredArrivalWindow(**base)


def test_apply_marks_slots_inside_window_rescheduled():
    win = _win(correlation_id="S2-A")
    result = tas_mock.apply_deferred_window(win)
    # tas_mock lazily mints 12 slots at 15-min cadence over ~3h from "now";
    # a 90-min window starting now must catch several BOOKED slots.
    assert result["applied_slots"] >= 1
    slots = {s["slot_id"]: s for s in tas_mock.list_slots("G-TEST")}
    for sid in result["window"]["applied_slots"]:
        assert slots[sid]["status"] == "RESCHEDULED"


def test_apply_is_idempotent_per_correlation_id():
    win = _win(correlation_id="S2-B", gate_id="G-TEST-B")
    tas_mock.apply_deferred_window(win)
    n_before = len(tas_mock.deferred_windows())
    tas_mock.apply_deferred_window(win)  # re-delivery must not duplicate
    assert len(tas_mock.deferred_windows()) == n_before


def test_booking_cap_enforced_then_released_outside_window():
    win = _win(correlation_id="S2-C", gate_id="G-TEST-C", slot_cap=2)
    tas_mock.apply_deferred_window(win)
    inside = win.window_start + timedelta(minutes=10)
    ok1, w1 = tas_mock.check_booking_allowed("G-TEST-C", inside)
    ok2, _ = tas_mock.check_booking_allowed("G-TEST-C", inside)
    ok3, w3 = tas_mock.check_booking_allowed("G-TEST-C", inside)
    assert ok1 and ok2, "bookings under the cap must pass"
    assert w1 is not None and w1["correlation_id"] == "S2-C"
    assert not ok3, "third booking must be refused (slot_cap=2)"
    assert w3["booked"] == 2
    # Outside the window the cap does not apply.
    outside = win.window_start + timedelta(minutes=win.window_min + 5)
    ok4, w4 = tas_mock.check_booking_allowed("G-TEST-C", outside)
    assert ok4 and w4 is None


def test_other_gate_not_affected():
    win = _win(correlation_id="S2-D", gate_id="G-TEST-D")
    tas_mock.apply_deferred_window(win)
    ok, w = tas_mock.check_booking_allowed("G-OTHER",
                                           win.window_start + timedelta(minutes=5))
    assert ok and w is None
