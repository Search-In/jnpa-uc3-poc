"""Unit tests for UC1 dashboard board mapping helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.marine.dashboard_boards import traffic_state, _berth_ui_state
from services.marine.projection import CallProjection

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def _proj(**kw) -> CallProjection:
    base = dict(
        call_id=1,
        vessel_name="TEST SHIP",
        is_in_port=False,
        is_at_berth=False,
        pilot_state="Pending",
        arrival_state="Pending",
    )
    base.update(kw)
    return CallProjection(**base)


def test_traffic_state_alongside_when_at_berth():
    at = _dt(2026, 6, 9, 8, 30)
    assert traffic_state(_proj(is_at_berth=True, is_in_port=True), at) == "alongside"


def test_traffic_state_departed_within_twelve_hours():
    at = _dt(2026, 6, 9, 8, 30)
    p = _proj(atd=_dt(2026, 6, 9, 6, 0))
    assert traffic_state(p, at) == "departed"


def test_traffic_state_anchored():
    at = _dt(2026, 6, 9, 8, 30)
    p = _proj(is_in_port=True, anchored_at=_dt(2026, 6, 8, 20, 0))
    assert traffic_state(p, at) == "at_anchorage"


def test_traffic_state_inbound_with_future_eta():
    at = _dt(2026, 6, 9, 8, 30)
    p = _proj(eta=_dt(2026, 6, 10, 8, 0))
    assert traffic_state(p, at) == "expected"


def test_berth_ui_state_working_vs_idle():
    assert _berth_ui_state("Occupied", "CARGO_OPERATION") == "occupied-working"
    assert _berth_ui_state("Occupied", "BERTH_ASSIGNED") == "occupied-idle"
    assert _berth_ui_state("Free", "") == "free"
