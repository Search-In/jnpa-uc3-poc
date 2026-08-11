"""Scenario I-A (vessel bunching) — arithmetic, objective handling, projection.

Two layers, matching ``test_cargo_simulation.py``:

* the scheduler and scorer as pure functions, against hand-computed expectations
  written out in the docstrings so a reviewer can check them without running
  anything;
* the scenario end-to-end over a fake repository, including the 6 August
  projection path, which is the case the Notice actually asks for.

The worked example used throughout — three vessels, one berth, all ready at the
start of the study day:

    ===  =========  =====  =====
    id   service    moves  berth
    ===  =========  =====  =====
    A    4 h        400    B1
    B    2 h        100    B1
    C    6 h        900    B1
    ===  =========  =====  =====

    FCFS       A(0->4) B(4->6) C(6->12)   waiting 0+4+6  = 10
    SPT        B(0->2) A(2->6) C(6->12)   waiting 0+2+6  =  8
    MAX_MOVES  C(0->6) A(6->10) B(10->12) waiting 0+6+10 = 16

SPT wins on waiting time; MAX_MOVES wins on moves cleared inside a 6-hour
horizon (900 against 500). That the ranking flips with the objective is the
whole point of the scenario, so it is asserted rather than assumed.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.cargo.simulation import REGISTRY, SimulationError  # noqa: E402
from services.cargo.simulation.base import QueryTrace  # noqa: E402
from services.cargo.simulation.projection import PROJECTION_ASSUMPTION_ID  # noqa: E402
from services.cargo.simulation import vessel_bunching as vb  # noqa: E402

DAY = datetime(2026, 8, 6, tzinfo=timezone.utc)


def _cand(name: str, *, service: float, moves: int, berth: str = "B1",
          ready_offset_h: float = 0.0, line: Optional[str] = None) -> dict:
    return {
        "berthing_record_id": None, "vessel_name": name, "voyage_number": f"V{name}",
        "terminal": "NSICT", "original_berth": berth, "shipping_line": line,
        "gross_moves": moves, "ready": DAY + timedelta(hours=ready_offset_h),
        "service_hours": service, "service_hours_assumed": False,
    }


THREE = [_cand("A", service=4, moves=400), _cand("B", service=2, moves=100),
         _cand("C", service=6, moves=900)]


# =========================================================== pure: scheduling
def test_fcfs_schedules_in_readiness_order():
    plan = vb.schedule(THREE, ["B1"], "FCFS")
    assert [r["vessel_name"] for r in plan] == ["A", "B", "C"]
    assert [r["waiting_hours"] for r in plan] == [0.0, 4.0, 6.0]


def test_spt_schedules_shortest_first():
    plan = vb.schedule(THREE, ["B1"], "SPT")
    assert [r["vessel_name"] for r in plan] == ["B", "A", "C"]
    assert [r["waiting_hours"] for r in plan] == [0.0, 2.0, 6.0]


def test_max_moves_schedules_largest_first():
    plan = vb.schedule(THREE, ["B1"], "MAX_MOVES")
    assert [r["vessel_name"] for r in plan] == ["C", "A", "B"]
    assert [r["waiting_hours"] for r in plan] == [0.0, 6.0, 10.0]


def test_spt_minimises_total_waiting():
    """The textbook result, and the reason SPT is one of the candidates."""
    totals = {
        o: vb.evaluate(vb.schedule(THREE, ["B1"], o),
                       horizon_end=DAY + timedelta(hours=24))["total_waiting_hours"]
        for o in ("FCFS", "SPT", "MAX_MOVES")
    }
    assert totals == {"FCFS": 10.0, "SPT": 8.0, "MAX_MOVES": 16.0}
    assert min(totals, key=totals.get) == "SPT"


def test_second_berth_absorbs_the_queue():
    """Two berths halve the contention — the scheduler must actually use both."""
    plan = vb.schedule(THREE, ["B1", "B2"], "FCFS")
    assert len({r["assigned_berth"] for r in plan}) == 2
    total = vb.evaluate(plan, horizon_end=DAY + timedelta(hours=24))
    assert total["total_waiting_hours"] < 10.0


def test_vessel_keeps_its_own_berth_when_free():
    """Among berths free at the same moment, a vessel's recorded berth wins, so a
    plan does not propose gratuitous reassignments."""
    cands = [_cand("A", service=4, moves=400, berth="B2")]
    plan = vb.schedule(cands, ["B1", "B2"], "FCFS")
    assert plan[0]["assigned_berth"] == "B2"
    assert plan[0]["berth_shift"] is False


def test_scheduling_is_deterministic():
    a = vb.schedule(THREE, ["B1", "B2"], "FCFS")
    b = vb.schedule(THREE, ["B1", "B2"], "FCFS")
    assert [(r["vessel_name"], r["assigned_berth"], r["start"]) for r in a] == \
           [(r["vessel_name"], r["assigned_berth"], r["start"]) for r in b]


def test_equal_candidates_break_ties_on_vessel_name():
    same = [_cand("Z", service=3, moves=1), _cand("A", service=3, moves=1)]
    assert [r["vessel_name"] for r in vb.schedule(same, ["B1"], "SPT")] == ["A", "Z"]


# ============================================================= pure: scoring
def test_moves_handled_counts_only_calls_finishing_inside_the_horizon():
    """6-hour horizon: FCFS and SPT clear 500 moves, MAX_MOVES clears 900."""
    horizon = DAY + timedelta(hours=6)
    handled = {o: vb.evaluate(vb.schedule(THREE, ["B1"], o),
                              horizon_end=horizon)["moves_handled"]
               for o in ("FCFS", "SPT", "MAX_MOVES")}
    assert handled == {"FCFS": 500, "SPT": 500, "MAX_MOVES": 900}


def test_objective_value_uses_the_shared_uc1_weights():
    """cost = 1.0*waiting + 2.0*tide_misses + 0.5*berth_shifts."""
    metrics = {"total_waiting_hours": 10.0, "tide_misses": 0, "berth_shifts": 4,
               "moves_handled": 500}
    assert vb.objective_value(metrics, "waiting_time") == 12.0


def test_moves_objective_is_maximised_not_minimised():
    assert vb.OBJECTIVES["moves_handled"][1] == "maximise"
    assert vb.OBJECTIVES["waiting_time"][1] == "minimise"


# ====================================================== scenario: end-to-end
class _WindowRepo:
    """Fake repository that can answer differently per window.

    Needed because the projection path queries the requested window first and the
    lookback second; a repo that returns the same rows for both cannot exercise
    it."""

    def __init__(self, by_window: list[list[dict]]) -> None:
        self._answers = list(by_window)
        self.windows: list[tuple] = []

    async def calls_with_moves(self, *, terminal=None, from_ts=None, to_ts=None):
        self.windows.append((from_ts, to_ts))
        rows = self._answers.pop(0) if self._answers else []
        return list(rows), QueryTrace(purpose="calls_with_moves", sql="SELECT 1",
                                      params={"from_ts": from_ts, "to_ts": to_ts},
                                      row_count=len(rows))


def _row(name: str, berth: str, start_h: float, end_h: float, moves: int,
         *, day: datetime = DAY) -> dict:
    return {
        "berthing_record_id": abs(hash(name)) % 10_000, "terminal": "NSICT",
        "vessel_name": name, "voyage_number": f"V{name}", "berth_number": berth,
        "shipping_line": None, "gross_moves": moves,
        "cargo_operation_start": day + timedelta(hours=start_h),
        "cargo_operation_end": day + timedelta(hours=end_h),
        "berthing_time": day + timedelta(hours=start_h),
    }


@pytest.mark.asyncio
async def test_measured_day_answers_without_projecting():
    repo = _WindowRepo([[_row("A", "B1", 0, 4, 400), _row("B", "B1", 1, 3, 100)]])
    res = await vb.run(repo, {"as_of": DAY, "objective": "waiting_time"})
    assert res.data_available is True
    assert res.result["coverage"]["basis"] == "MEASURED"
    assert len(repo.windows) == 1, "a populated window must not trigger a lookback"


@pytest.mark.asyncio
async def test_projects_forward_when_the_requested_day_is_empty():
    """The 6 August case: nothing in the window, so carry the last day forward."""
    still_working = [_row("LATE", "B1", -6, 6, 800, day=DAY)]   # ends after DAY
    repo = _WindowRepo([[], still_working])
    res = await vb.run(repo, {"as_of": DAY})

    assert len(repo.windows) == 2, "an empty window must trigger the lookback"
    coverage = res.result["coverage"]
    assert coverage["basis"] == "PROJECTED"
    assert coverage["assumption_id"] == PROJECTION_ASSUMPTION_ID
    assert coverage["carried_calls"] == 1
    assert any(PROJECTION_ASSUMPTION_ID in a.reason for a in res.assumptions)
    assert any("PROJECTED day" in n for n in res.notes)


@pytest.mark.asyncio
async def test_blocks_when_there_is_nothing_to_project_from():
    """Empty window AND empty history is a refusal, not a fabricated day."""
    res = await vb.run(_WindowRepo([[], []]), {"as_of": DAY})
    assert res.data_available is False
    assert res.result["coverage"]["basis"] == "NONE"


@pytest.mark.asyncio
async def test_objective_is_named_and_alternatives_are_costed():
    """Notice I-A: state the objective, and cost an alternative against it."""
    repo = _WindowRepo([[_row("A", "B1", 0, 4, 400), _row("B", "B1", 0, 2, 100),
                         _row("C", "B1", 0, 6, 900)]])
    res = await vb.run(repo, {"as_of": DAY, "objective": "waiting_time"})

    assert res.result["objective"]["id"] == "waiting_time"
    assert res.result["objective"]["direction"] == "minimise"
    assert res.result["recommended"]["ordering"] == "SPT"

    alternatives = res.result["alternatives"]
    assert {a["ordering"] for a in alternatives} == {"FCFS", "MAX_MOVES", "LINE_PRIORITY"}
    assert all(a["cost_vs_recommended"] >= 0 for a in alternatives)
    fcfs = next(a for a in alternatives if a["ordering"] == "FCFS")
    assert fcfs["is_baseline"] is True
    assert fcfs["cost_vs_recommended"] == 2.0      # 10 - 8
    assert res.figures["improvement_vs_baseline"] == 2.0


@pytest.mark.asyncio
async def test_changing_the_objective_changes_the_recommendation():
    """The scenario's reason for existing: the 'best' order depends on the basis."""
    rows = [_row("A", "B1", 0, 4, 400), _row("B", "B1", 0, 2, 100),
            _row("C", "B1", 0, 6, 900)]
    waiting = await vb.run(_WindowRepo([list(rows)]),
                           {"as_of": DAY, "objective": "waiting_time",
                            "horizon_hours": 6})
    moves = await vb.run(_WindowRepo([list(rows)]),
                         {"as_of": DAY, "objective": "moves_handled",
                          "horizon_hours": 6})
    assert waiting.result["recommended"]["ordering"] == "SPT"
    assert moves.result["recommended"]["ordering"] == "MAX_MOVES"


@pytest.mark.asyncio
async def test_line_priority_without_lines_is_declared_not_disguised():
    repo = _WindowRepo([[_row("A", "B1", 0, 4, 400), _row("B", "B1", 0, 2, 100)]])
    res = await vb.run(repo, {"as_of": DAY, "objective": "line_priority"})
    assert any("LINE_PRIORITY degrades" in n for n in res.notes)


@pytest.mark.asyncio
async def test_unknown_objective_is_rejected():
    with pytest.raises(SimulationError):
        await vb.run(_WindowRepo([[]]), {"as_of": DAY, "objective": "vibes"})


@pytest.mark.asyncio
async def test_terminal_imbalance_is_surfaced():
    """The Notice's premise — uneven distribution between terminals."""
    rows = [_row("A", "B1", 0, 4, 400), _row("B", "B1", 0, 2, 100)]
    rows[1]["terminal"] = "APMT"
    rows.append({**_row("C", "B1", 0, 3, 50), "terminal": "NSICT"})
    res = await vb.run(_WindowRepo([rows]), {"as_of": DAY})
    assert res.result["load_by_terminal"] == {"NSICT": 2, "APMT": 1}
    assert res.figures["busiest_terminal"] == "NSICT"
    assert any(r["action"] == "REBALANCE_ACROSS_TERMINALS"
               for r in res.recommendations)


# ============================================== Notice §1 contract (AC-25)
@pytest.mark.asyncio
async def test_answer_carries_method_assumptions_and_queries():
    repo = _WindowRepo([[_row("A", "B1", 0, 4, 400), _row("B", "B1", 0, 2, 100)]])
    res = await vb.run(repo, {"as_of": DAY})
    payload = res.to_dict()
    assert payload["method"].strip()
    assert payload["assumptions"], "Notice §1.c — assumptions must be declared"
    assert payload["queries"], "Notice §1.d — the working must be traceable"
    assert payload["figures"], "Notice §1.b — figures must support the result"
    # The objective must be one of the declared assumptions, by name.
    assert any(a["field"] == "objective" for a in payload["assumptions"])


def test_scenario_is_registered():
    assert vb.SCENARIO in REGISTRY
    assert REGISTRY[vb.SCENARIO] is vb
