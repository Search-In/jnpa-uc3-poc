"""Bidder-proposed scenarios N-1, N-2, N-3.

These are not requested by JNPA. Each exists because it covers a capability class
that none of the twenty-one requested obligations does:

* **N-1 channel closure** — one shared asset throttling both directions at once
* **N-2 yard feedback** — a closed loop rather than a one-way cascade
* **N-3 degraded gate** — a digital failure, and the only scenario measuring
  RECOVERY rather than impact

Same standard as the requested five: pure arithmetic checked against
hand-computed expectations, plus the Notice §1 envelope, plus honest refusal when
the data is not there.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.cargo.simulation import CATALOG, REGISTRY  # noqa: E402
from services.cargo.simulation import channel_closure as cc  # noqa: E402
from services.cargo.simulation import degraded_gate as dg  # noqa: E402
from services.cargo.simulation import yard_feedback as yf  # noqa: E402
from services.cargo.simulation.base import QueryTrace  # noqa: E402

T0 = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _trace(purpose: str, rows: int = 0) -> QueryTrace:
    return QueryTrace(purpose=purpose, sql="SELECT 1", params={}, row_count=rows)


# =========================================================== N-3 degraded gate
def _profile(arrivals: list[int], start: datetime = T0) -> list[dict]:
    return [{"bucket": start + timedelta(hours=i), "arrivals": n, "completed": n}
            for i, n in enumerate(arrivals)]


def test_queue_builds_only_while_degraded():
    """10/h arriving, 10/h normal capacity, 4/h degraded for hours 1-2.

    h0 normal:   demand 10, served 10, queue 0
    h1 degraded: demand 10, served  4, queue 6
    h2 degraded: demand 16, served  4, queue 12
    h3 normal:   demand 22, served 10, queue 12
    """
    profile = _profile([10, 10, 10, 10])
    rows = dg.simulate_queue(profile, normal_rate=10, degraded_rate=4,
                             outage_from=T0 + timedelta(hours=1),
                             outage_to=T0 + timedelta(hours=3))
    assert [r["queue"] for r in rows] == [0.0, 6.0, 12.0, 12.0]
    assert [r["degraded"] for r in rows] == [False, True, True, False]


def test_baseline_arm_never_queues_when_capacity_meets_demand():
    rows = dg.baseline_queue(_profile([10, 10, 10]), normal_rate=10)
    assert all(r["queue"] == 0.0 for r in rows)


def test_queue_drains_once_capacity_exceeds_arrivals():
    """After the outage, spare capacity eats the backlog."""
    profile = _profile([10, 10, 2, 2])
    rows = dg.simulate_queue(profile, normal_rate=10, degraded_rate=2,
                             outage_from=T0, outage_to=T0 + timedelta(hours=2))
    assert rows[1]["queue"] == 16.0
    assert rows[-1]["queue"] == 0.0


def test_recovery_hour_is_none_when_the_queue_never_clears():
    profile = _profile([20, 20, 20])
    rows = dg.simulate_queue(profile, normal_rate=10, degraded_rate=2,
                             outage_from=T0, outage_to=T0 + timedelta(hours=1))
    assert dg.recovery_hour(rows, outage_to=T0 + timedelta(hours=1),
                            target_queue=0.0) is None


class _GateRepo:
    def __init__(self, profile: list[dict], caps: Optional[list[dict]] = None):
        self._profile, self._caps = profile, caps or []

    async def gate_hourly_profile(self, **_kw):
        return list(self._profile), _trace("gate_hourly_profile", len(self._profile))

    async def gate_event_hourly(self, **_kw):
        return [], _trace("gate_event_hourly")

    async def tas_hourly_capacity(self, **_kw):
        return list(self._caps), _trace("tas_hourly_capacity", len(self._caps))


@pytest.mark.asyncio
async def test_degraded_gate_reports_backlog_and_recovery():
    repo = _GateRepo(_profile([10] * 6), caps=[{"slot_capacity": 10}])
    res = await dg.run(repo, {"from_ts": T0, "to_ts": T0 + timedelta(hours=6),
                              "outage_start": T0 + timedelta(hours=1),
                              "outage_hours": 2, "degraded_fraction": 0.4})
    assert res.data_available is True
    assert res.figures["normal_sustained_rate"] == 10.0
    assert res.figures["degraded_rate"] == 4.0
    assert res.figures["peak_queue_with_outage"] > 0
    assert res.figures["peak_queue_without_outage"] == 0.0
    payload = res.to_dict()
    assert payload["assumptions"] and payload["queries"] and payload["method"]


@pytest.mark.asyncio
async def test_degraded_gate_refuses_without_a_profile():
    res = await dg.run(_GateRepo([]), {"from_ts": T0, "to_ts": T0 + timedelta(hours=6)})
    assert res.data_available is False
    assert any("no gate arrivals" in n for n in res.notes)


# ========================================================= N-1 channel closure
def _call(vessel: str, berth: str, start_h: float, end_h: float,
          moves: int = 100) -> dict:
    return {"vessel_name": vessel, "voyage_number": f"V{vessel}",
            "berth_number": berth, "terminal": "NSICT", "gross_moves": moves,
            "cargo_operation_start": T0 + timedelta(hours=start_h),
            "cargo_operation_end": T0 + timedelta(hours=end_h),
            "berthing_time": T0 + timedelta(hours=start_h)}


def test_berth_is_held_once_its_vessel_finishes_under_closure():
    calls = [_call("A", "B1", -2, 1)]
    assert cc.berth_states(calls, T0)["B1"] == "working"
    assert cc.berth_states(calls, T0 + timedelta(hours=2))["B1"] == "held"


def test_berth_lock_when_every_berth_is_occupied_and_one_is_held():
    """Two berths, both occupied; A finishes at +1 so from +1 nothing can free."""
    calls = [_call("A", "B1", -2, 1), _call("B", "B2", -2, 20)]
    timeline = cc.walk_closure(calls, ["B1", "B2"], closure_from=T0,
                               closure_to=T0 + timedelta(hours=4))
    assert timeline[0]["berth_locked"] is False       # both still working
    assert timeline[2]["berth_locked"] is True        # A held, B working, none free
    assert timeline[2]["held"] == 1 and timeline[2]["free"] == 0


def test_no_berth_lock_while_a_berth_stays_free():
    calls = [_call("A", "B1", -2, 1)]
    timeline = cc.walk_closure(calls, ["B1", "B2"], closure_from=T0,
                               closure_to=T0 + timedelta(hours=4))
    assert not any(r["berth_locked"] for r in timeline)


def test_sailing_order_policies_differ():
    held = [_call("SMALL", "B1", -4, -1, moves=50),
            _call("BIG", "B2", -2, 0, moves=900)]
    assert [c["vessel_name"] for c in cc.sailing_order(held, "LONGEST_HELD_FIRST")] \
        == ["SMALL", "BIG"]
    assert [c["vessel_name"] for c in cc.sailing_order(held, "LARGEST_FIRST")] \
        == ["BIG", "SMALL"]


class _CallsRepo:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def calls_with_moves(self, **_kw):
        return list(self._rows), _trace("calls_with_moves", len(self._rows))


@pytest.mark.asyncio
async def test_channel_closure_reports_lock_and_costs_an_alternative_order():
    rows = [_call("A", "B1", -2, 1), _call("B", "B2", -2, 2)]
    res = await cc.run(_CallsRepo(rows), {"as_of": T0, "closure_hours": 6})
    assert res.data_available is True
    assert res.figures["berth_lock_reached"] is True
    assert res.figures["hours_to_berth_lock"] is not None
    alt = res.result["sailing_order"]["alternative"]
    assert "cost_vs_recommended" in alt
    payload = res.to_dict()
    assert payload["assumptions"] and payload["queries"]


# ========================================================== N-2 yard feedback
def test_productivity_is_untouched_below_the_threshold():
    assert yf.productivity_factor(0.5, threshold=0.85, slope=0.4) == 1.0
    assert yf.productivity_factor(0.85, threshold=0.85, slope=0.4) == 1.0


def test_productivity_falls_linearly_above_the_threshold():
    """Halfway between 0.85 and 1.0 costs half the slope."""
    assert yf.productivity_factor(0.925, threshold=0.85, slope=0.4) == \
        pytest.approx(0.8, abs=0.01)
    assert yf.productivity_factor(1.0, threshold=0.85, slope=0.4) == \
        pytest.approx(0.6, abs=0.01)


def test_productivity_never_reaches_zero():
    """A dreadful yard slows a berth; it does not stop it, and a zero factor
    would make the loop divide by nothing."""
    assert yf.productivity_factor(5.0, threshold=0.85, slope=0.99) >= 0.1


def test_shortfall_fills_the_yard_faster_than_the_baseline():
    kwargs = dict(opening_occupancy=1000.0, capacity=2000.0,
                  nominal_discharge=200.0, nominal_evacuation=200.0,
                  threshold=0.85, slope=0.4, days=10)
    baseline = yf.simulate(evacuation_drop=0.0, **kwargs)
    stressed = yf.simulate(evacuation_drop=0.5, **kwargs)
    assert baseline[-1]["utilisation"] < stressed[-1]["utilisation"]
    assert stressed[-1]["productivity_factor"] < 1.0


def test_utilisation_never_exceeds_capacity():
    """A yard cannot be 148% full. Occupancy is capped and the surplus is tracked
    as blocked discharge — the vessel-side backlog — instead."""
    rows = yf.simulate(opening_occupancy=1000.0, capacity=2000.0,
                       nominal_discharge=200.0, nominal_evacuation=200.0,
                       evacuation_drop=0.5, threshold=0.85, slope=0.4, days=60)
    assert max(r["utilisation"] for r in rows) <= 1.0
    assert rows[-1]["yard_full"] is True
    assert rows[-1]["discharge_blocked"] > 0


def test_saturating_regime_when_productivity_floor_stays_above_evacuation():
    """discharge floor 200*0.6 = 120/day against 100/day leaving: no equilibrium.

    This is the case that first exposed the flaw — the model used to let
    utilisation run to 148% and call it 'converged'."""
    assert yf.regime(nominal_discharge=200.0, nominal_evacuation=200.0,
                     evacuation_drop=0.5, slope=0.4) == "SATURATING"
    rows = yf.simulate(opening_occupancy=1000.0, capacity=2000.0,
                       nominal_discharge=200.0, nominal_evacuation=200.0,
                       evacuation_drop=0.5, threshold=0.85, slope=0.4, days=60)
    assert yf.saturation_day(rows) is not None


def test_converging_regime_settles_below_capacity():
    """A deeper productivity collapse (slope 0.7) pulls discharge to 60/day,
    below the 100/day leaving, so a fixed point below capacity exists."""
    assert yf.regime(nominal_discharge=200.0, nominal_evacuation=200.0,
                     evacuation_drop=0.5, slope=0.7) == "CONVERGING"
    rows = yf.simulate(opening_occupancy=1000.0, capacity=2000.0,
                       nominal_discharge=200.0, nominal_evacuation=200.0,
                       evacuation_drop=0.5, threshold=0.85, slope=0.7, days=90)
    last_ten = [r["utilisation"] for r in rows[-10:]]
    assert max(last_ten) - min(last_ten) < 0.05
    assert max(last_ten) < 1.0
    assert yf.saturation_day(rows) is None


def test_blocked_discharge_only_appears_once_the_yard_is_full():
    rows = yf.simulate(opening_occupancy=100.0, capacity=5000.0,
                       nominal_discharge=200.0, nominal_evacuation=200.0,
                       evacuation_drop=0.5, threshold=0.85, slope=0.4, days=5)
    assert all(r["discharge_blocked"] == 0 for r in rows)
    assert not any(r["yard_full"] for r in rows)


def test_tipping_day_is_the_first_crossing():
    rows = yf.simulate(opening_occupancy=1600.0, capacity=2000.0,
                       nominal_discharge=200.0, nominal_evacuation=200.0,
                       evacuation_drop=1.0, threshold=0.85, slope=0.4, days=10)
    day = yf.tipping_day(rows, 0.85)
    assert day is not None and rows[day - 1]["utilisation"] > 0.85
    assert all(r["utilisation"] <= 0.85 for r in rows[:day - 1])


class _TrafficRepo:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def rail_road_daily(self, **_kw):
        return list(self._rows), _trace("rail_road_daily", len(self._rows))


@pytest.mark.asyncio
async def test_yard_feedback_refuses_without_traffic_rows():
    res = await yf.run(_TrafficRepo([]), {"from_date": date(2026, 8, 1),
                                          "to_date": date(2026, 8, 5)})
    assert res.data_available is False
    assert any("perf_daily_traffic" in n for n in res.notes)


@pytest.mark.asyncio
async def test_yard_feedback_refuses_when_volumes_are_empty():
    """Rows present but every volume column null is still not an answer."""
    rows = [{"report_date": date(2026, 8, 1), "imp_teus": None,
             "total_teus": None, "rail_total_teus": None}]
    res = await yf.run(_TrafficRepo(rows), {"from_date": date(2026, 8, 1),
                                            "to_date": date(2026, 8, 5)})
    assert res.data_available is False


@pytest.mark.asyncio
async def test_yard_feedback_reports_a_tipping_point():
    rows = [{"report_date": date(2026, 8, d), "imp_teus": 1000.0,
             "total_teus": 1000.0, "rail_total_teus": 300.0} for d in range(1, 6)]
    res = await yf.run(_TrafficRepo(rows), {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 5),
        "evacuation_drop_pct": 0.6, "yard_capacity_teu": 5000.0})
    assert res.data_available is True
    assert res.figures["final_utilisation_pct"] >= \
        res.figures["final_utilisation_pct_baseline"]
    payload = res.to_dict()
    assert payload["assumptions"] and payload["queries"] and payload["figures"]
    # The assumed curve must be declared, not buried.
    assert any(a["field"] == "occupancy_to_productivity_curve"
               for a in payload["assumptions"])
    # Which regime it landed in is the headline, so it must be reported.
    assert res.figures["regime"] in ("CONVERGING", "SATURATING")
    assert any("REGIME:" in n for n in res.notes)


# ================================================================== catalogue
def test_all_three_are_registered_and_marked_bidder_proposed():
    for module in (cc, yf, dg):
        assert module.SCENARIO in REGISTRY
        entry = next(c for c in CATALOG if c["scenario"] == module.SCENARIO)
        assert entry["proposed_by"] == "bidder", \
            "a scenario JNPA did not ask for must say so in the catalogue"


def test_catalogue_covers_every_registered_scenario():
    """A scenario cannot be added without describing itself."""
    assert {c["scenario"] for c in CATALOG} == set(REGISTRY)
