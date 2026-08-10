"""Unit tests for the six-arrival-times ladder (UC1-019 / UI-025)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.marine.arrival_times import assemble_arrival_times

IST = timezone(timedelta(hours=5, minutes=30))


def _dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


def test_tss_amber_ladder_flags_31_day_gap():
    """Hero vessel: CALINF ETA 28-06 00:16, VESARR anchorage/pilot/berth on 29-07."""
    call = {
        "call_id": 48,
        "vcn": "INNSA1NF0S0776",
        "via_no": "S0776",
        "vessel_name": "TSS AMBER",
        "voyage_no": "2626",
        "imo_no": "9241918",
        "eta": _dt(2026, 6, 28, 0, 16),
        "source_note": "CALINF-AMBER-DEMO",
        "ata": _dt(2026, 7, 29, 21, 24),
        "atc": None,
        "atd": None,
    }
    events = [
        {"event_type": "ANCHORED", "event_ts": _dt(2026, 7, 29, 5, 18),
         "source_note": "PCS15072600001"},
        {"event_type": "PILOT_BOARDED", "event_ts": _dt(2026, 7, 29, 19, 30),
         "source_note": "PCS15072600001", "extras": {"pilot_name": "KULDEEP RAWAT"}},
        {"event_type": "BERTHED", "event_ts": _dt(2026, 7, 29, 21, 24),
         "source_note": "PCS15072600001"},
    ]
    out = assemble_arrival_times(call, events)
    by = {r["key"]: r for r in out["arrival_times"]}
    assert len(out["arrival_times"]) == 6
    assert by["proforma_eta"]["value"] is None
    assert "no source" in (by["proforma_eta"]["note"] or "").lower()
    assert by["declared_eta"]["value"] == call["eta"]
    assert "CALINF" in (by["declared_eta"]["source"] or "")
    assert by["last_reported_eta"]["value"] is None
    assert by["at_anchorage"]["value"] == events[0]["event_ts"]
    assert "VESARR" in (by["at_anchorage"]["source"] or "")
    assert by["pilot_boarding"]["value"] == events[1]["event_ts"]
    assert "KULDEEP RAWAT" in (by["pilot_boarding"]["note"] or "")
    assert by["first_line"]["value"] == events[2]["event_ts"]
    assert by["first_line"]["derived"] is True
    assert out["anomalies"], "31-day gap must be flagged"
    assert out["anomalies"][0]["code"] == "eta_vs_anchorage_gap"
    assert out["anomalies"][0]["days"] >= 28


def test_missing_rows_stay_honest_without_fabricated_values():
    out = assemble_arrival_times(
        {"call_id": 1, "eta": None, "ata": None, "atc": None, "atd": None},
        [],
    )
    assert all(r["value"] is None for r in out["arrival_times"])
    assert out["anomalies"] == []
