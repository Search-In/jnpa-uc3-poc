"""Tests for the pure KPI engine (shared/jnpa_shared/kpi.py).

These assert the value/target/delta/on-target arithmetic for fixed inputs so the
number an evaluator sees on the dashboard can never silently drift from the
definition in docs/KPI_DEFINITIONS.md. No infrastructure required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from jnpa_shared import kpi  # noqa: E402


def test_all_acceptance_kpis_have_targets():
    for key in ("gate_queue_wait", "gate_txn_time", "trt_empty_ecd", "tat_inside_port",
                "queue_length", "avg_dwell", "gate_throughput"):
        assert key in kpi.KPI_TARGETS


def test_lower_is_better_on_target_and_delta_sign():
    # Wait time 6.0 min vs target 8.0 (baseline 14.5) -> on target, improved.
    r = kpi.compute_kpi("gate_queue_wait", 6.0)
    assert r.on_target is True
    assert r.delta_pct < 0  # improvement reads as a negative change vs baseline
    # exact: (6 - 14.5)/14.5*100 = -58.62...
    assert r.delta_pct == pytest.approx(-58.62, abs=0.05)
    assert r.unit == "min"
    assert r.direction == "lower_is_better"


def test_lower_is_better_off_target():
    r = kpi.compute_kpi("gate_txn_time", 4.5)  # target 3.0
    assert r.on_target is False


def test_higher_is_better_on_target_and_delta_sign():
    # Throughput 66 vph vs target 60 (baseline 44) -> on target, improved (+).
    r = kpi.compute_kpi("gate_throughput", 66.0)
    assert r.on_target is True
    assert r.delta_pct > 0
    assert r.delta_pct == pytest.approx(50.0, abs=0.05)  # (66-44)/44*100
    assert r.direction == "higher_is_better"


def test_higher_is_better_off_target():
    r = kpi.compute_kpi("gate_throughput", 50.0)  # target 60
    assert r.on_target is False


def test_trend_appends_current_value():
    r = kpi.compute_kpi("avg_dwell", 11.0, trend=[15.0, 13.0, 12.0])
    assert r.trend[-1] == 11.0
    assert len(r.trend) == 4


def test_trend_no_duplicate_when_last_matches():
    r = kpi.compute_kpi("avg_dwell", 12.0, trend=[15.0, 13.0, 12.0])
    assert r.trend == [15.0, 13.0, 12.0]


def test_to_dict_uses_camel_case_delta():
    d = kpi.compute_kpi("gate_queue_wait", 6.0).to_dict()
    assert "deltaPct" in d
    assert "onTarget" in d
    assert d["onTarget"] is True


def test_unknown_key_raises():
    with pytest.raises(KeyError):
        kpi.compute_kpi("not_a_kpi", 1.0)


# --- aggregation helpers ----------------------------------------------------

def test_gate_queue_wait_min_from_seconds():
    # 300s and 540s -> mean 420s -> 7.0 min
    assert kpi.gate_queue_wait_min([300, 540]) == pytest.approx(7.0)


def test_gate_queue_wait_min_empty():
    assert kpi.gate_queue_wait_min([]) == 0.0


def test_gate_throughput_vph_extrapolates_window():
    # 20 cleared in a 30-min window -> 40 vph
    assert kpi.gate_throughput_vph(20, 30.0) == pytest.approx(40.0)


def test_gate_throughput_vph_zero_window():
    assert kpi.gate_throughput_vph(20, 0.0) == 0.0


def test_trt_empty_ecd_min():
    assert kpi.trt_empty_ecd_min([2700, 2700]) == pytest.approx(45.0)  # 45 min each


def test_kpi_strip_skips_missing_and_unknown():
    strip = kpi.kpi_strip({"gate_queue_wait": 6.0, "bogus": 1.0})
    keys = [row["key"] for row in strip]
    assert keys == ["gate_queue_wait"]  # only the known+present key
