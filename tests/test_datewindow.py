"""The shared date-window primitive.

Both failure modes pinned here return a plausible WRONG answer rather than an
error, so they are invisible without a test: an exclusive `to_date` silently
drops the last day, and a UTC-anchored boundary silently shifts every edge by
5h30 against a corpus stamped in IST.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from gateway.datewindow import (IST, MAX_WINDOW_DAYS, DateWindow, date_window,
                                validate_window)


def test_open_window_is_inert():
    """Adding the dependency to an endpoint must not change an unfiltered call."""
    w = DateWindow()
    assert w.is_open
    assert w.sql("created_at") == ("", {})
    assert w.describe() is None


def test_to_date_includes_the_whole_last_day():
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 6))
    # A single-day window must span midnight-to-midnight, not collapse to zero.
    assert w.end_ts() - w.start_ts() == timedelta(days=1)


def test_bounds_are_anchored_in_ist_not_utc():
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    assert w.start_ts().utcoffset() == timedelta(hours=5, minutes=30)
    assert w.start_ts() == datetime(2026, 6, 6, 0, 0, tzinfo=IST)
    # 06-Jun 00:00 IST is 05-Jun 18:30 UTC — the 5h30 a UTC anchor would lose.
    assert w.start_ts().astimezone(timezone.utc) == datetime(2026, 6, 5, 18, 30,
                                                             tzinfo=timezone.utc)
    assert w.end_ts() == datetime(2026, 6, 13, 0, 0, tzinfo=IST)


def test_sql_is_half_open_and_fully_bound():
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    clause, params = w.sql("truck_in_time")
    assert clause == " AND truck_in_time >= :dw_from AND truck_in_time < :dw_to_excl"
    assert set(params) == {"dw_from", "dw_to_excl"}
    # No literal date may be interpolated into the SQL text.
    assert "2026" not in clause


def test_sql_prefix_allows_two_windows_in_one_query():
    a, _ = DateWindow(from_date=date(2026, 6, 6)).sql("a_ts", prefix="w1")
    b, _ = DateWindow(from_date=date(2026, 7, 1)).sql("b_ts", prefix="w2")
    assert ":w1_from" in a and ":w2_from" in b


def test_half_bounded_windows():
    lo, lo_p = DateWindow(from_date=date(2026, 8, 1)).sql("ts")
    assert "dw_from" in lo_p and "dw_to_excl" not in lo_p
    hi, hi_p = DateWindow(to_date=date(2026, 8, 3)).sql("ts")
    assert "dw_to_excl" in hi_p and "dw_from" not in hi_p


def test_inverted_window_is_rejected():
    with pytest.raises(HTTPException) as e:
        validate_window(date(2026, 8, 3), date(2026, 8, 1))
    assert e.value.status_code == 400
    assert e.value.detail["error"] == "invalid_window"


def test_oversized_window_is_rejected():
    start = date(2026, 1, 1)
    validate_window(start, start + timedelta(days=MAX_WINDOW_DAYS))  # at the limit: fine
    with pytest.raises(HTTPException):
        validate_window(start, start + timedelta(days=MAX_WINDOW_DAYS + 1))


def test_dependency_returns_a_window_and_validates():
    w = date_window(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    assert isinstance(w, DateWindow) and not w.is_open
    assert date_window(from_date=None, to_date=None).is_open
    with pytest.raises(HTTPException):
        date_window(from_date=date(2026, 6, 12), to_date=date(2026, 6, 6))


def test_describe_is_evidence_grade():
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    assert w.describe() == "2026-06-06 to 2026-06-12 (IST, inclusive)"


def test_the_golden_thread_window_covers_its_records():
    """06-12 June 2026 is the only week with manifests, EDI and gate documents
    together; the T1 gate-out at 12-06 04:46 IST must fall inside it."""
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    gate_out = datetime(2026, 6, 12, 4, 46, tzinfo=IST)
    assert w.start_ts() <= gate_out < w.end_ts()


def test_inclusive_bound_for_legacy_lte_comparisons():
    """Some repositories compare `col <= :bound`. Handing those the half-open
    bound would admit a record stamped exactly midnight the following day."""
    w = DateWindow(from_date=date(2026, 6, 6), to_date=date(2026, 6, 12))
    assert w.end_ts_inclusive() < w.end_ts()
    assert w.end_ts() - w.end_ts_inclusive() == timedelta(microseconds=1)
    # 12-Jun 23:59:59.999999 IST is inside; 13-Jun 00:00 IST is not.
    assert w.end_ts_inclusive() == datetime(2026, 6, 12, 23, 59, 59, 999999, tzinfo=IST)
    assert DateWindow().end_ts_inclusive() is None
