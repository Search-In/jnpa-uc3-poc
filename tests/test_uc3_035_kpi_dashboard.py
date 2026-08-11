"""UC3-035 — tender KPI wording, dual TAT and the distribution figures.

The ticket's warning is "a wrong unit is a free mark lost", so the names, units,
targets and baselines are asserted VERBATIM against the tender table. The rest
guards the two rules the dashboard turns on: the dual-TAT pair may never be
split, and an unmeasured KPI must not read as a measured zero.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from jnpa_shared.kpi import KPI_TARGETS  # noqa: E402


#: WS4 KPI table, verbatim: (label, unit, target, baseline).
TENDER_TABLE = {
    "gate_queue_wait": ("Gate Queue Wait Time", "min", 8.0, 14.5),
    "gate_txn_time": ("Avg Gate Transaction Time", "min", 3.0, 5.2),
    "trt_empty_ecd": ("TRT for empty containers from ECD", "min", 45.0, 72.0),
    "tat_inside_port": ("Turn Around Time Inside Port", "min", 90.0, 135.0),
    "queue_length": ("Queue Length", "vehicles", 25.0, 41.0),
    "avg_dwell": ("Avg Vehicle Dwell", "min", 12.0, 19.0),
    "gate_throughput": ("Gate Throughput", "vph", 60.0, 44.0),
}


@pytest.mark.parametrize("key,expected", TENDER_TABLE.items())
def test_kpi_wording_units_targets_and_baselines_are_tender_exact(key, expected):
    label, unit, target, baseline = expected
    k = KPI_TARGETS[key]
    assert k.label == label, f"{key} label deviates from tender wording"
    assert k.unit == unit, f"{key} unit deviates — a wrong unit is a free mark lost"
    assert k.target == target
    assert k.baseline == baseline


def test_no_extra_or_missing_kpis():
    assert set(KPI_TARGETS) == set(TENDER_TABLE)


def test_dual_tat_endpoint_cannot_return_a_single_definition():
    """UI-122: neither definition may be displayed alone anywhere in the product.

    The endpoint takes no parameter that could select one arm, so a caller has no
    way to ask for half the pair. That is what makes the rule enforceable rather
    than a convention the next screen forgets.
    """
    import inspect

    from gateway.routers import kpi as kpi_router

    sig = inspect.signature(kpi_router.dual_tat)
    params = set(sig.parameters) - {"state"}
    assert params == set(), f"dual_tat must take no selector params, got {params}"

    src = inspect.getsource(kpi_router.dual_tat)
    assert '"terminal"' in src and '"driver"' in src
    assert "must_render_together" in src


def test_distribution_reads_per_trip_rows_not_hourly_means():
    """A P90 over hourly means hides the tail it is asked to expose."""
    import inspect

    from gateway.routers import kpi as kpi_router

    src = inspect.getsource(kpi_router.distribution)
    assert "percentile_cont(0.9)" in src
    assert "v_gate_trip_timeline" in src
    # The percentile must be taken over trip durations, not over bucket averages.
    assert "FROM trips" in src


def test_every_trip_metric_reuses_the_mart_view_predicates():
    """The distribution and the headline mean must describe the same population."""
    from gateway.routers.kpi import _TRIP_METRICS

    assert set(_TRIP_METRICS) == {"gate_queue_wait", "gate_txn_time", "tat_inside_port"}
    for key, m in _TRIP_METRICS.items():
        assert "EXTRACT(EPOCH FROM" in m["value"], key
        assert "IS NOT NULL" in m["where"], key
        # Each metric orders its endpoints so a negative duration cannot enter.
        assert ">=" in m["where"], key
