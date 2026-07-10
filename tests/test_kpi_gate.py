"""Event-driven gate KPI wiring (Appendix-C).

Covers the pieces that turn raw gate lifecycle events into the dashboard KPI
values: the shared engine's source/provenance fields, and the gateway's
bucket-aggregation helper that builds a live KpiResult from the KPI views.

The SQL views themselves are exercised end-to-end against the running stack in
the runtime verification step; here we test the pure logic around them.
"""
from __future__ import annotations

from jnpa_shared import kpi as kpi_engine


def test_compute_kpi_carries_source_and_n():
    live = kpi_engine.compute_kpi("gate_queue_wait", 6.5, source="live", n=42)
    d = live.to_dict()
    assert d["source"] == "live"
    assert d["n"] == 42
    assert d["value"] == 6.5

    base = kpi_engine.compute_kpi("gate_queue_wait", 14.5, source="baseline")
    bd = base.to_dict()
    assert bd["source"] == "baseline"
    assert bd["n"] == 0


def test_compute_kpi_defaults_to_live_source():
    # Back-compat: callers that don't pass a source still get a valid result.
    r = kpi_engine.compute_kpi("gate_txn_time", 2.9)
    assert r.to_dict()["source"] == "live"


def test_kpi_from_buckets_trips_weighted_mean():
    from gateway.routers.kpi import _kpi_from_buckets

    # newest-first, as the view returns (ORDER BY bucket DESC). The weighted mean
    # must weight by trips, not treat buckets equally.
    rows = [
        {"bucket": "t3", "wait_min": 6.0, "trips": 10},
        {"bucket": "t2", "wait_min": 9.0, "trips": 2},
        {"bucket": "t1", "wait_min": 3.0, "trips": 0},  # empty bucket -> ignored
    ]
    res = _kpi_from_buckets("gate_queue_wait", rows, "wait_min")
    assert res is not None
    d = res.to_dict()
    # (6*10 + 9*2) / 12 = 78/12 = 6.5
    assert d["value"] == 6.5
    assert d["source"] == "live"
    assert d["n"] == 12
    # trend is chronological (oldest -> newest) from usable buckets, then the
    # engine appends the headline value if it differs from the last point.
    assert d["trend"][0] == 9.0  # oldest usable bucket first
    assert d["trend"][1] == 6.0


def test_kpi_from_buckets_none_when_no_data():
    from gateway.routers.kpi import _kpi_from_buckets

    assert _kpi_from_buckets("gate_txn_time", [], "txn_min") is None
    # Rows present but all zero-trip -> still no live value.
    rows = [{"bucket": "t1", "txn_min": None, "trips": 0}]
    assert _kpi_from_buckets("gate_txn_time", rows, "txn_min") is None
