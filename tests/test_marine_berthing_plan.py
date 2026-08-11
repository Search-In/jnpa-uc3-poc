"""Unit tests for the 5-day berthing plan honesty assembly (UC1-024 / UI-028)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.marine.berthing_plan import (
    CONFIRMED_SOURCE,
    INDICATIVE_SOURCE,
    assemble_berthing_plan,
    build_confirmed_entry,
    confirmed_span,
    resolve_window,
)

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_resolve_window_uses_latest_actual_when_at_omitted():
    latest = _dt(2026, 7, 22)
    start, end, anchor = resolve_window(at=None, days=5, latest_actual=latest)
    assert anchor == latest
    assert end - start == timedelta(days=5)


def test_active_cargo_without_departure_names_end_as_estimate():
    """SELETAR-style: cargo_operation_end used, but still flagged estimate (no ATD)."""
    anchor = _dt(2026, 7, 20, 4, 30)
    row = {
        "id": 39,
        "terminal": "NSICT",
        "vessel_name": "MAERSK SELETAR",
        "berth_number": "CB05",
        "berthing_time": _dt(2026, 7, 20, 4, 36),
        "departure_time": None,
        "cargo_operation_end": _dt(2026, 7, 20, 23, 59),
        "status": "CARGO_OPERATION",
        "source_file": "NSICT_2026-07-20.pdf",
        "voyage_number": "X",
        "imo_number": "",
        "shipping_line": "",
        "ata": None,
        "eta": None,
    }
    start, end, estimated = confirmed_span(row, anchor=anchor)
    assert start == row["berthing_time"]
    assert end == row["cargo_operation_end"]
    assert estimated is True
    entry = build_confirmed_entry(row, anchor=anchor)
    assert entry is not None
    assert entry["kind"] == "confirmed"
    assert entry["berth_code"] == "CB05"
    assert CONFIRMED_SOURCE in entry["source"]
    assert "NSICT_2026-07-20.pdf" in entry["source"]
    assert entry["end_estimated"] is True


def test_estimated_end_capped_at_48h():
    """Long open stays must not draw multi-day bars that break what-if drag."""
    from services.marine.berthing_plan import _MAX_ESTIMATED_SPAN
    anchor = _dt(2026, 6, 5, 8, 30)
    row = {
        "id": 1,
        "terminal": "APMT",
        "vessel_name": "ARAYA BHUM",
        "berth_number": "APM01",
        "berthing_time": _dt(2026, 5, 25, 4, 0),
        "departure_time": None,
        "cargo_operation_end": None,
        "status": "CARGO_OPERATION",
        "source_file": "APMT.pdf",
        "voyage_number": "1",
        "imo_number": "",
        "shipping_line": "",
        "ata": None,
        "eta": None,
    }
    start, end, estimated = confirmed_span(row, anchor=anchor)
    assert estimated is True
    assert end - start <= _MAX_ESTIMATED_SPAN
    # Natural capped bar ends before the June pin — pin-local rescue keeps it on board.
    win_start, win_end = anchor, anchor + timedelta(days=5)
    entry = build_confirmed_entry(row, anchor=anchor, win_start=win_start, win_end=win_end)
    assert entry is not None
    assert entry["end_estimated"] is True
    assert entry["end_ts"] - entry["start_ts"] <= timedelta(hours=24)
    assert "alongside since" in entry["source"]


def test_assemble_marks_confirmed_and_indicative_distinctly():
    anchor = _dt(2026, 7, 20, 4, 30)
    win_start, win_end = anchor, anchor + timedelta(days=5)
    confirmed_rows = [{
        "id": 39,
        "terminal": "NSICT",
        "vessel_name": "MAERSK SELETAR",
        "berth_number": "CB05",
        "berthing_time": _dt(2026, 7, 20, 4, 36),
        "departure_time": None,
        "cargo_operation_end": _dt(2026, 7, 20, 23, 59),
        "status": "CARGO_OPERATION",
        "source_file": "NSICT_2026-07-20.pdf",
        "voyage_number": "1",
        "imo_number": "",
        "shipping_line": "MAERSK",
        "ata": None,
        "eta": None,
    }]
    call_rows = [{
        "call_id": 58,
        "vcn": "INNSA1NF0S1080",
        "via_no": "S1080",
        "imo_no": "1",
        "vessel_name": "NEXT CALLER",
        "voyage_no": "V1",
        "eta": _dt(2026, 7, 23, 6, 0),
        "etd": None,
        "etb": None,
        "status": "VCN Allotted",
        "terminal_code": "NSICT",
        "berth_code": None,
    }]
    entries = assemble_berthing_plan(
        confirmed_rows=confirmed_rows,
        call_rows=call_rows,
        win_start=win_start,
        win_end=win_end,
        anchor=anchor,
    )
    kinds = {e["kind"] for e in entries}
    assert kinds == {"confirmed", "indicative"}
    seletar = next(e for e in entries if e["vessel_name"] == "MAERSK SELETAR")
    assert seletar["kind"] == "confirmed"
    assert seletar["berth_code"] == "CB05"
    assert seletar["end_ts"] - seletar["start_ts"] <= timedelta(hours=48)
    twin = next(e for e in entries if e["kind"] == "indicative")
    assert twin["source"] == INDICATIVE_SOURCE
    assert twin["end_estimated"] is True
    # Twin follow-on lands on the same berth after the confirmed bar.
    assert twin["berth_code"] == "CB05"
    assert twin["start_ts"] >= seletar["end_ts"]
