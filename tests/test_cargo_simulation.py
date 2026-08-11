"""Tests for the UC-3 what-if simulation layer (/api/cargo/simulate).

Same two layers as test_cargo.py:

* Pure scenario arithmetic against an in-memory fake repository — every figure in
  a JNPA answer is checked against a hand-computed expectation, so a regression in
  the maths fails loudly rather than producing a plausible wrong number.
* Router behaviour through Starlette's TestClient with the service swapped via
  ``app.dependency_overrides``.

Plus the architectural invariant the whole layer rests on: the simulation
repository cannot write.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from services.cargo.simulation import (  # noqa: E402
    CATALOG,
    REGISTRY,
    SimulationError,
    SimulationRepository,
    SimulationResult,
    SimulationService,
    SimulationWriteAttempt,
)
from services.cargo.simulation.base import QueryTrace  # noqa: E402
from services.cargo.simulation.berth_cascade import cascade  # noqa: E402
from services.cargo.simulation.gate_slotting import flatten, percentile  # noqa: E402

UTC = timezone.utc
AUG2 = datetime(2026, 8, 2, tzinfo=UTC)
AUG6 = datetime(2026, 8, 6, tzinfo=UTC)


def _ts(day: int, hour: int) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=UTC)


def _trace(purpose: str = "fake", rows: int = 0) -> QueryTrace:
    return QueryTrace(purpose=purpose, sql="SELECT 1", params={}, row_count=rows)


# --------------------------------------------------------------------------- fake
class FakeSimRepo:
    """In-memory stand-in for SimulationRepository with identical contracts.

    Every method returns ``(rows, QueryTrace)`` exactly as the real one does, so a
    scenario cannot tell the difference — and a scenario that forgets to publish
    its trace fails the assertions below."""

    def __init__(self, **overrides: Any) -> None:
        self.data: dict[str, list[dict]] = {
            "berth_queue": [],
            "calls_with_moves": [],
            "gate_hourly_profile": [],
            "gate_event_hourly": [],
            "tas_hourly_capacity": [],
            "rail_road_daily": [],
            "evacuation_mode_split": [],
            "vehicle_trips": [],
            "cargo_flows": [],
            "pendency_snapshot": [],
        }
        self.data.update(overrides)
        self.calls: list[str] = []

    def _answer(self, key: str):
        self.calls.append(key)
        rows = self.data.get(key) or []
        return list(rows), _trace(key, len(rows))

    async def berth_queue(self, **_kw):
        return self._answer("berth_queue")

    async def calls_with_moves(self, **_kw):
        return self._answer("calls_with_moves")

    async def gate_hourly_profile(self, **_kw):
        return self._answer("gate_hourly_profile")

    async def gate_event_hourly(self, **_kw):
        return self._answer("gate_event_hourly")

    async def tas_hourly_capacity(self, **_kw):
        return self._answer("tas_hourly_capacity")

    async def rail_road_daily(self, **_kw):
        return self._answer("rail_road_daily")

    async def evacuation_mode_split(self, **_kw):
        return self._answer("evacuation_mode_split")

    async def vehicle_trips(self, **_kw):
        return self._answer("vehicle_trips")

    async def cargo_flows(self, **_kw):
        return self._answer("cargo_flows")

    async def pendency_snapshot(self, **_kw):
        return self._answer("pendency_snapshot")


# Three calls back-to-back on berth B1 (4h each) + one on B2 that must not move.
BERTH_CALLS = [
    {"id": 1, "terminal": "NSICT", "vessel_name": "VESSEL ONE", "voyage_number": "V1",
     "berth_number": "B1", "cargo_operation_start": _ts(2, 8),
     "cargo_operation_end": _ts(2, 12), "eta": _ts(2, 7), "ata": _ts(2, 7),
     "berthing_time": _ts(2, 8), "departure_time": _ts(2, 13), "status": "COMPLETED"},
    {"id": 2, "terminal": "NSICT", "vessel_name": "VESSEL TWO", "voyage_number": "V2",
     "berth_number": "B1", "cargo_operation_start": _ts(2, 12),
     "cargo_operation_end": _ts(2, 16), "eta": _ts(2, 11), "ata": _ts(2, 11),
     "berthing_time": _ts(2, 12), "departure_time": _ts(2, 17), "status": "EXPECTED"},
    {"id": 3, "terminal": "NSICT", "vessel_name": "VESSEL THREE", "voyage_number": "V3",
     "berth_number": "B1", "cargo_operation_start": _ts(2, 16),
     "cargo_operation_end": _ts(2, 20), "eta": _ts(2, 15), "ata": _ts(2, 15),
     "berthing_time": _ts(2, 16), "departure_time": _ts(2, 21), "status": "EXPECTED"},
    {"id": 4, "terminal": "NSICT", "vessel_name": "OTHER BERTH", "voyage_number": "V4",
     "berth_number": "B2", "cargo_operation_start": _ts(2, 9),
     "cargo_operation_end": _ts(2, 15), "eta": _ts(2, 8), "ata": _ts(2, 8),
     "berthing_time": _ts(2, 9), "departure_time": _ts(2, 16), "status": "EXPECTED"},
]


def _moves_rows() -> list[dict]:
    """The berth calls joined to move counts: 400 moves over 4h = 100 moves/hour."""
    rows = []
    for call, moves in zip(BERTH_CALLS, (400, 200, 320, 600)):
        rows.append({**{k: v for k, v in call.items() if k != "id"},
                     "berthing_record_id": call["id"],
                     "vcn": f"VCN-{call['voyage_number']}",
                     "discharge_moves": moves, "load_moves": 0, "restow_moves": None,
                     "gross_moves": moves, "cranes_deployed": 2,
                     "data_origin": "DERIVED", "source_note": "counted from EDI"})
    return rows


# 6 active hours; 400 arrivals total, peak 150.
GATE_PROFILE = [
    {"bucket": _ts(1, 6), "arrivals": 20, "completed": 20, "unique_trucks": 20,
     "avg_tat_min": 45},
    {"bucket": _ts(1, 7), "arrivals": 50, "completed": 50, "unique_trucks": 48,
     "avg_tat_min": 52},
    {"bucket": _ts(1, 8), "arrivals": 150, "completed": 100, "unique_trucks": 140,
     "avg_tat_min": 95},
    {"bucket": _ts(1, 9), "arrivals": 100, "completed": 90, "unique_trucks": 95,
     "avg_tat_min": 70},
    {"bucket": _ts(1, 10), "arrivals": 60, "completed": 60, "unique_trucks": 58,
     "avg_tat_min": 48},
    {"bucket": _ts(1, 11), "arrivals": 20, "completed": 20, "unique_trucks": 20,
     "avg_tat_min": 40},
]

#: GATE_PROFILE scaled x5 (2,000 trips), for the modal-shift tests only.
#:
#: RAIL_DAILY carries 4,000 road TEU across the window. Against GATE_PROFILE's 400
#: trips that implies **10 TEU per truck trip**, which no truck can carry, and the
#: plausibility guard now rejects it. The arithmetic those tests exercise is
#: unchanged; only the fixture is made physical. 4,000 TEU / 2,000 trips = 2.0
#: TEU/trip, i.e. a fleet of 40ft boxes.
GATE_PROFILE_ROAD = [{**h, "arrivals": h["arrivals"] * 5,
                      "completed": h["completed"] * 5,
                      "unique_trucks": h["unique_trucks"] * 5}
                     for h in GATE_PROFILE]

RAIL_DAILY = [
    {"report_date": date(2026, 8, 1), "terminal_code": "NSICT", "total_teus": 2000,
     "imp_teus": 1200, "exp_teus": 800, "rakes": 4, "rail_dis_teus": 200,
     "rail_ldg_teus": 100, "rail_total_teus": 300},
    {"report_date": date(2026, 8, 2), "terminal_code": "NSICT", "total_teus": 1500,
     "imp_teus": 900, "exp_teus": 600, "rakes": 3, "rail_dis_teus": 150,
     "rail_ldg_teus": 100, "rail_total_teus": 250},
    {"report_date": date(2026, 8, 3), "terminal_code": "NSICT", "total_teus": 1300,
     "imp_teus": 800, "exp_teus": 500, "rakes": 3, "rail_dis_teus": 150,
     "rail_ldg_teus": 100, "rail_total_teus": 250},
]

VEHICLE_TRIPS = [
    {"transporter": "ALPHA LOGISTICS", "truck_no": "MH04AB1234",
     "trip_date": date(2026, 8, 1), "trips": 3, "containers": 3, "avg_tat_min": 60},
    {"transporter": "ALPHA LOGISTICS", "truck_no": "MH04AB5678",
     "trip_date": date(2026, 8, 1), "trips": 2, "containers": 2, "avg_tat_min": 65},
    {"transporter": "BETA CARRIERS", "truck_no": "MH04CD1111",
     "trip_date": date(2026, 8, 1), "trips": 1, "containers": 1, "avg_tat_min": 70},
]


# ============================================================ read-only invariant
def test_simulation_read_only_rejects_writes():
    """The guard the whole layer rests on: a simulation may not write."""
    for sql in ("INSERT INTO core.cargo VALUES (1)",
                "UPDATE core.cargo SET is_released = true",
                "DELETE FROM core.cargo",
                "DROP TABLE core.cargo",
                "TRUNCATE core.cargo",
                "ALTER TABLE core.cargo ADD COLUMN x int",
                "CREATE TABLE t (a int)"):
        with pytest.raises(SimulationWriteAttempt):
            SimulationRepository.assert_read_only(sql)


def test_simulation_read_only_rejects_write_hidden_in_a_cte():
    """Postgres allows `WITH x AS (DELETE ... RETURNING *) SELECT * FROM x`, which
    starts with WITH and still writes. The guard must catch it."""
    with pytest.raises(SimulationWriteAttempt):
        SimulationRepository.assert_read_only(
            "WITH gone AS (DELETE FROM core.cargo RETURNING *) SELECT * FROM gone")


def test_simulation_read_only_allows_reads():
    SimulationRepository.assert_read_only("SELECT 1")
    SimulationRepository.assert_read_only(
        "  WITH x AS (SELECT 1 AS n) SELECT n FROM x")


def test_every_repository_statement_is_a_read():
    """Architectural invariant, in the style of test_cargo.py's single-writer test:
    EVERY SQL constant on the simulation repository passes the read-only guard, so
    the layer cannot acquire a write path without this test failing."""
    statements = [v for k, v in vars(SimulationRepository).items()
                  if k.endswith("_SQL") and isinstance(v, str)]
    assert len(statements) >= 8, "expected the repository's SQL constants to be found"
    for sql in statements:
        SimulationRepository.assert_read_only(sql)


def test_nullable_filters_are_cast_for_asyncpg():
    """Regression (found against the live RDS, 2026-08-06).

    `(:terminal IS NULL OR terminal = :terminal)` makes asyncpg raise
    AmbiguousParameterError — Postgres cannot infer the parameter's type from a
    bare NULL comparison. The fix is an explicit cast, and it must be written
    `CAST(:x AS text)`: `:x::text` makes SQLAlchemy's text() read the second
    colon as a bind marker and fail with a syntax error. Both forms were shipped
    and both broke every scenario, silently, via the fail-soft path."""
    statements = [v for k, v in vars(SimulationRepository).items()
                  if k.endswith("_SQL") and isinstance(v, str)]
    for sql in statements:
        # `:param::type` breaks; `round(x)::numeric` is fine and is used.
        bad = re.findall(r":\w+::\w+", sql)
        assert not bad, (
            f"cast directly on a bind param breaks SQLAlchemy text() — "
            f"use CAST(:x AS type): {bad}")
        for match in re.finditer(r":(\w+) IS NULL", sql):
            param = match.group(1)
            assert f"CAST(:{param} AS" in sql, (
                f"nullable filter :{param} needs an explicit CAST for asyncpg")


def test_a_failed_query_is_not_reported_as_no_data():
    """Regression: an empty table and a broken query both yield zero rows. Before
    this, a failure surfaced as 'no calls in this window' — a confidently wrong
    answer, the one thing this layer must never produce."""
    res = SimulationResult(scenario="x", method="y")
    res.trace(QueryTrace(purpose="berth queue", sql="SELECT 1", params={},
                         row_count=0, error='relation "core.x" does not exist'))
    out = res.to_dict()
    assert out["data_available"] is False
    assert any("QUERY FAILED" in n for n in out["notes"])
    assert out["queries"][0]["error"].startswith("relation")


def test_a_genuinely_empty_result_is_not_flagged_as_a_failure():
    res = SimulationResult(scenario="x", method="y")
    res.trace(QueryTrace(purpose="berth queue", sql="SELECT 1", params={}, row_count=0))
    out = res.to_dict()
    assert out["data_available"] is True
    assert not any("QUERY FAILED" in n for n in out["notes"])


def test_simulation_repository_never_opens_a_transaction():
    """A read-only layer has nothing to commit. `engine.begin()` would open one."""
    source = (REPO_ROOT / "services" / "cargo" / "simulation" / "repository.py").read_text()
    code = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    # The call form, not the bare word — the module docstring explains why
    # engine.begin() is avoided, and that sentence must not fail its own test.
    assert "get_engine(self._dsn).begin()" not in code
    assert "get_engine(self._dsn).connect()" in code


# ============================================================== cascade arithmetic
def test_cascade_pushes_the_whole_berth_queue():
    """Pure function: a 6h overrun on the first of three back-to-back 4h calls
    delays both followers by exactly 6h."""
    plan = cascade(BERTH_CALLS, target_index=0, delta_hours=6.0)
    by_vessel = {r["vessel_name"]: r for r in plan}
    assert by_vessel["VESSEL ONE"]["delay_hours"] == 6.0
    assert by_vessel["VESSEL TWO"]["delay_hours"] == 6.0
    assert by_vessel["VESSEL THREE"]["delay_hours"] == 6.0
    # VESSEL TWO originally started at 12:00; VESSEL ONE now ends at 18:00.
    assert by_vessel["VESSEL TWO"]["new_start"] == _ts(2, 18)
    assert by_vessel["VESSEL THREE"]["new_start"] == _ts(2, 22)


def test_cascade_leaves_other_berths_untouched():
    plan = cascade(BERTH_CALLS, target_index=0, delta_hours=6.0)
    other = next(r for r in plan if r["vessel_name"] == "OTHER BERTH")
    assert other["delay_hours"] == 0.0
    assert other["new_start"] == other["original_start"]


def test_cascade_absorbs_an_overrun_that_fits_in_the_gap():
    """A 1h overrun on a call with a 4h gap behind it displaces nobody."""
    spaced = [BERTH_CALLS[0],
              {**BERTH_CALLS[1], "cargo_operation_start": _ts(2, 20),
               "cargo_operation_end": _ts(2, 23), "berthing_time": _ts(2, 20)}]
    plan = cascade(spaced, target_index=0, delta_hours=1.0)
    assert plan[1]["delay_hours"] == 0.0


# ================================================================ I-B berth cascade
@pytest.mark.asyncio
async def test_berth_cascade_scenario_end_to_end():
    svc = SimulationService(repository=FakeSimRepo(berth_queue=BERTH_CALLS))
    out = await svc.run("berth-cascade", {
        "terminal": "NSICT", "as_of": AUG2, "delay_hours": 6.0,
        "vessel_name": "VESSEL ONE"})

    assert out["data_available"] is True
    assert out["figures"]["calls_displaced"] == 2
    assert out["figures"]["cumulative_delay_hours"] == 12.0
    assert out["figures"]["max_single_delay_hours"] == 6.0
    assert out["figures"]["target_delay_hours"] == 6.0
    displaced = {d["vessel"] for d in out["result"]["displaced_calls"]}
    assert displaced == {"VESSEL TWO", "VESSEL THREE"}
    # The JNPA §1 contract is present and populated.
    assert out["method"] and out["assumptions"] and out["queries"]
    assert any(a["field"] == "berth_exclusivity" for a in out["assumptions"])
    assert any(a["field"] == "delay_hours" and a["value"] == 6.0
               for a in out["assumptions"])
    assert out["recommendations"]


@pytest.mark.asyncio
async def test_berth_cascade_reports_missing_data_instead_of_inventing_it():
    svc = SimulationService(repository=FakeSimRepo(berth_queue=[]))
    out = await svc.run("berth-cascade", {"terminal": "NSICT", "as_of": AUG2})
    assert out["data_available"] is False
    assert out["figures"] == {}
    assert any("no calls" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_berth_cascade_declares_an_unnamed_target_as_an_assumption():
    svc = SimulationService(repository=FakeSimRepo(berth_queue=BERTH_CALLS))
    out = await svc.run("berth-cascade", {"terminal": "NSICT", "as_of": AUG2})
    assert any(a["field"] == "target_call" and a["source"] == "ASSUMED"
               for a in out["assumptions"])


# ========================================================== II-B crane productivity
@pytest.mark.asyncio
async def test_crane_productivity_baseline_and_reduction():
    """400 moves / 4h = 100 moves/hour. A 25% cut gives 75 moves/hour, so the same
    400 moves take 4 / 0.75 = 5.33h — an increase of 1.33h."""
    svc = SimulationService(repository=FakeSimRepo(calls_with_moves=_moves_rows()))
    out = await svc.run("crane-productivity", {
        "terminal": "NSICT", "as_of": AUG2, "reduction_pct": 0.25,
        "vessel_name": "VESSEL ONE"})

    f = out["figures"]
    assert f["baseline_moves_per_hour"] == 100.0
    assert f["reduced_moves_per_hour"] == 75.0
    assert f["baseline_turnaround_hours"] == 4.0
    assert f["reduced_turnaround_hours"] == 5.33
    assert f["turnaround_increase_hours"] == 1.33
    # The queue behind it inherits exactly that overrun.
    assert f["calls_displaced"] == 2
    assert f["cumulative_berth_delay_hours"] == 2.66
    assert f["total_delay_hours"] == 3.99


@pytest.mark.asyncio
async def test_crane_productivity_declares_derived_move_counts():
    """A manifest-derived move count is a proxy, and the answer must say so."""
    svc = SimulationService(repository=FakeSimRepo(calls_with_moves=_moves_rows()))
    out = await svc.run("crane-productivity", {"as_of": AUG2,
                                               "vessel_name": "VESSEL ONE"})
    gross = next(a for a in out["assumptions"] if a["field"] == "gross_moves")
    assert gross["source"] == "DERIVED"
    assert "excludes restows" in gross["reason"]


@pytest.mark.asyncio
async def test_crane_productivity_without_move_counts_reports_not_derivable():
    """The audit's central II-B finding: no numerator, no productivity. The answer
    must say that rather than substituting a fleet average."""
    rows = [{**r, "gross_moves": None, "discharge_moves": None, "load_moves": None,
             "data_origin": None} for r in _moves_rows()]
    svc = SimulationService(repository=FakeSimRepo(calls_with_moves=rows))
    out = await svc.run("crane-productivity", {"as_of": AUG2})
    assert out["data_available"] is False
    assert "moves_per_hour" not in out["figures"]
    assert any("not derivable" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_crane_productivity_skips_calls_without_an_operation_window():
    """A call with no operation window has an UNKNOWN rate, not a zero one."""
    rows = _moves_rows()
    rows[1] = {**rows[1], "cargo_operation_start": None, "cargo_operation_end": None,
               "berthing_time": None, "departure_time": None}
    svc = SimulationService(repository=FakeSimRepo(calls_with_moves=rows))
    out = await svc.run("crane-productivity", {"as_of": AUG2,
                                               "vessel_name": "VESSEL ONE"})
    two = next(c for c in out["result"]["baseline_by_call"]
               if c["vessel_name"] == "VESSEL TWO")
    assert two["moves_per_hour"] is None
    assert two["derivable"] is False


# ================================================================ II-A modal shift
@pytest.mark.asyncio
async def test_modal_shift_sizes_the_extra_road_load():
    """Rail 800 TEU over the window; 20% = 160 TEU shifted. Road TEU is
    4800 - 800 = 4000 across 2,000 observed trips => 2 TEU/trip => 80 extra trips.

    (Fixture changed from GATE_PROFILE's 400 trips, which implied 10 TEU per truck
    trip — physically impossible, and now rejected by the plausibility guard. The
    arithmetic under test is identical; only the data is made real.)"""
    repo = FakeSimRepo(rail_road_daily=RAIL_DAILY,
                       gate_hourly_profile=GATE_PROFILE_ROAD)
    svc = SimulationService(repository=repo)
    out = await svc.run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3),
        "shift_pct": 0.20, "sustained_rate": 500})

    f = out["figures"]
    assert f["rail_teus_in_window"] == 800.0
    assert f["shifted_teus"] == 160.0
    assert f["baseline_trips"] == 2000
    assert f["teu_per_trip"] == 2.0
    assert f["additional_truck_trips"] == 80
    assert f["shifted_trips"] == 2080
    # The added trips are apportioned to every hour and sum exactly.
    assert sum(h["added"] for h in out["result"]["shifted_profile"]) == 80


@pytest.mark.asyncio
async def test_modal_shift_identifies_the_first_constraint():
    repo = FakeSimRepo(rail_road_daily=RAIL_DAILY, gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3),
        "sustained_rate": 100})
    first = out["result"]["first_constraint"]
    assert first is not None
    assert first["constraint"] == "GATE_THROUGHPUT"
    # 08:00 is the 150-arrival peak — the earliest hour over a 100/h ceiling.
    assert first["hour"].startswith("2026-08-01T08:00")
    assert out["result"]["gate_absorbs_load"] is False


@pytest.mark.asyncio
async def test_modal_shift_reports_absorption_when_capacity_is_ample():
    repo = FakeSimRepo(rail_road_daily=RAIL_DAILY, gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3),
        "sustained_rate": 1000})
    assert out["result"]["gate_absorbs_load"] is True
    assert out["result"]["first_constraint"] is None
    assert any(r["action"] == "ABSORBED" for r in out["recommendations"])


@pytest.mark.asyncio
async def test_modal_shift_declares_the_teu_conversion():
    repo = FakeSimRepo(rail_road_daily=RAIL_DAILY,
                       gate_hourly_profile=GATE_PROFILE_ROAD)
    out = await SimulationService(repository=repo).run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    teu = next(a for a in out["assumptions"] if a["field"] == "teu_per_trip")
    assert teu["source"] == "DERIVED"
    assert any(a["field"] == "shift_arrival_shape" for a in out["assumptions"])


@pytest.mark.asyncio
async def test_modal_shift_rejects_an_impossible_teu_per_trip():
    """The guard, end to end.

    GATE_PROFILE's 400 trips against 4,000 road TEU implies 10 TEU per truck
    trip. No truck carries ten TEU, so the conversion must be rejected, demoted
    to ASSUMED, and explained — never used to size the road load, which it would
    understate fivefold."""
    repo = FakeSimRepo(rail_road_daily=RAIL_DAILY, gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    teu = next(a for a in out["assumptions"] if a["field"] == "teu_per_trip")
    assert teu["source"] == "ASSUMED"
    assert teu["value"] == 1.0
    assert "REJECTED" in teu["reason"]
    assert any("rejected as implausible" in n for n in out["notes"])


@pytest.mark.asyncio
async def test_modal_shift_without_rail_volume_reports_missing_data():
    repo = FakeSimRepo(rail_road_daily=[], gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("modal-shift", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    assert out["data_available"] is False


# =============================================================== III-A gate slotting
def test_percentile_is_nearest_rank_and_deterministic():
    assert percentile([1, 2, 3, 4, 5], 0.9) == 5.0
    # Half-up, not Python's banker's rounding: rank = floor(0.5*3 + 0.5) = 2.
    assert percentile([10, 20, 30, 40], 0.5) == 30.0
    # The six-hour case that motivated it: rank = floor(0.9*5 + 0.5) = 5, so the
    # top value is chosen. round(4.5) would have given 4 and picked the fifth.
    assert percentile([20, 20, 50, 60, 90, 100], 0.9) == 100.0
    assert percentile([], 0.9) is None


def test_flatten_caps_each_hour_and_spills_forward():
    profile = [{"bucket": _ts(1, 8), "arrivals": 150},
               {"bucket": _ts(1, 9), "arrivals": 20},
               {"bucket": _ts(1, 10), "arrivals": 10}]
    plan = flatten(profile, 100)
    assert plan["hours"][0]["slotted"] == 100      # capped
    assert plan["hours"][0]["deferred_out"] == 50
    assert plan["hours"][1]["slotted"] == 70       # 20 + 50 absorbed
    assert plan["hours"][1]["deferred_in"] == 50
    assert plan["unplaced"] == 0


def test_flatten_reports_what_does_not_fit():
    profile = [{"bucket": _ts(1, 8), "arrivals": 300},
               {"bucket": _ts(1, 9), "arrivals": 100}]
    plan = flatten(profile, 100)
    assert plan["unplaced"] == 200


@pytest.mark.asyncio
async def test_gate_slotting_characterises_and_flattens_the_peak():
    repo = FakeSimRepo(gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0), "sustained_rate": 100})

    f = out["figures"]
    assert f["total_arrivals"] == 400
    assert f["observed_peak"] == 150
    assert f["sustained_rate_per_hour"] == 100.0
    assert f["saturated_hours"] == 1          # only 08:00 exceeds 100
    assert f["excess_arrivals"] == 50.0
    assert f["slotted_peak"] == 100
    assert f["peak_reduction"] == 50.0
    assert f["peak_reduction_pct"] == 33.3
    assert out["result"]["arrival_pattern"]["shape"] == "PEAKED"
    assert out["result"]["arrival_pattern"]["peak_arrivals"] == 150


@pytest.mark.asyncio
async def test_gate_slotting_derives_the_sustained_rate_from_completions():
    """With no explicit rate and no TAS slots, the p90 of observed COMPLETIONS is
    used — and the response says which basis it chose and why."""
    repo = FakeSimRepo(gate_hourly_profile=GATE_PROFILE)
    out = await SimulationService(repository=repo).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0)})
    rate = next(a for a in out["assumptions"] if a["field"] == "gate_sustained_rate")
    assert rate["source"] == "DERIVED"
    assert "COMPLETIONS" in rate["reason"]
    assert rate["value"] == 100.0     # p90 of [20,50,100,90,60,20]


@pytest.mark.asyncio
async def test_gate_slotting_prefers_declared_tas_capacity():
    """A policy figure beats an inference, and is labelled MEASURED.

    The declared capacity must exceed the observed peak (GATE_PROFILE peaks at
    150/h) — a gate that demonstrably passed more than its declaration is not
    running to policy, and that case is covered separately below."""
    repo = FakeSimRepo(gate_hourly_profile=GATE_PROFILE,
                       tas_hourly_capacity=[
                           {"bucket": _ts(1, 8), "slot_capacity": 200, "slot_booked": 40,
                            "windows": 1},
                           {"bucket": _ts(1, 9), "slot_capacity": 200, "slot_booked": 40,
                            "windows": 1}])
    out = await SimulationService(repository=repo).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0)})
    rate = next(a for a in out["assumptions"] if a["field"] == "gate_sustained_rate")
    assert rate["source"] == "MEASURED"
    assert rate["value"] == 200.0


@pytest.mark.asyncio
async def test_gate_slotting_rejects_a_declaration_below_the_observed_peak():
    """The stub case, end to end.

    JNPA's core.tas_appointment declares 10 trucks/hour across 16 rows while the
    same window shows a peak of 284. Taking the declaration reported 21 of 24
    hours saturated and 92% of trucks unplaceable. Observed throughput is a floor
    on capacity, so the declaration must be set aside and the reason stated."""
    repo = FakeSimRepo(gate_hourly_profile=GATE_PROFILE,
                       tas_hourly_capacity=[
                           {"bucket": _ts(1, 8), "slot_capacity": 10, "slot_booked": 4,
                            "windows": 1}])
    out = await SimulationService(repository=repo).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0)})
    rate = next(a for a in out["assumptions"] if a["field"] == "gate_sustained_rate")
    assert rate["source"] == "DERIVED"
    assert rate["value"] != 10.0
    assert "NOT used" in rate["reason"]
    assert "floor on capacity" in rate["reason"]


@pytest.mark.asyncio
async def test_gate_slotting_falls_back_to_gate_events():
    repo = FakeSimRepo(gate_hourly_profile=[], gate_event_hourly=[
        {"bucket": _ts(1, 8), "arrivals": 40, "gate_in": 30, "gate_out": 28,
         "unique_trucks": 38}])
    out = await SimulationService(repository=repo).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0)})
    assert out["data_available"] is True
    assert any(a["field"] == "arrival_source" and a["value"] == "core.gate_event"
               for a in out["assumptions"])


@pytest.mark.asyncio
async def test_gate_slotting_without_arrivals_reports_missing_data():
    out = await SimulationService(repository=FakeSimRepo()).run("gate-slotting", {
        "from_ts": _ts(1, 0), "to_ts": _ts(2, 0)})
    assert out["data_available"] is False


# ============================================================== III-B driver shortage
@pytest.mark.asyncio
async def test_driver_shortage_throughput_effect():
    """Trips 3 / 2 / 1 fall to floor(x * 2/3) = 2 / 1 / 0: 6 trips become 3."""
    repo = FakeSimRepo(vehicle_trips=VEHICLE_TRIPS)
    out = await SimulationService(repository=repo).run("driver-shortage", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3),
        "state_date": date(2026, 8, 4)})

    f = out["figures"]
    assert f["baseline_trips"] == 6
    assert f["reduced_trips"] == 3
    assert f["trips_lost"] == 3
    assert f["throughput_loss_pct"] == 50.0
    assert f["vehicles_active"] == 3
    assert out["result"]["state_date"] == "2026-08-04"


@pytest.mark.asyncio
async def test_driver_shortage_ranks_exposure_two_ways():
    repo = FakeSimRepo(vehicle_trips=VEHICLE_TRIPS)
    out = await SimulationService(repository=repo).run("driver-shortage", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    exposed = out["result"]["exposed_transporters"]
    # ALPHA runs 5 of the 6 trips, so it loses the most in absolute terms.
    assert exposed["by_absolute_loss"][0]["transporter"] == "ALPHA LOGISTICS"
    assert exposed["by_absolute_loss"][0]["trips_lost"] == 2
    # Structural ranking is a different question and reports trips/vehicle-day.
    assert exposed["by_structural_dependence"][0]["trips_per_vehicle_day"] >= 1.0


@pytest.mark.asyncio
async def test_driver_shortage_projects_the_backlog_onto_the_state_date():
    repo = FakeSimRepo(vehicle_trips=VEHICLE_TRIPS, pendency_snapshot=[
        {"evacuation_mode": "ROAD", "lifecycle_status": "YARD_ASSIGNED",
         "containers": 40},
        {"evacuation_mode": "RAIL", "lifecycle_status": "RAKE_ASSIGNED",
         "containers": 10}])
    out = await SimulationService(repository=repo).run("driver-shortage", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3),
        "state_date": date(2026, 8, 4)})
    state = out["result"]["state_on_report_date"]
    assert state["already_pending_in_port"] == 50
    assert state["containers_not_evacuated"] == out["figures"]["containers_not_evacuated"]
    assert state["projected_total_awaiting_evacuation"] == 50 + state["containers_not_evacuated"]
    # A rail backlog exists, so the divert-to-rail strategy is recommended.
    assert any(r["action"] == "DIVERT_TO_RAIL" for r in out["recommendations"])


@pytest.mark.asyncio
async def test_driver_shortage_states_its_priority_rule():
    """"Show how best evacuation strategy is determined" — the rule must be
    explicit, not implied by the ordering of the output."""
    repo = FakeSimRepo(vehicle_trips=VEHICLE_TRIPS)
    out = await SimulationService(repository=repo).run("driver-shortage", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    rule = next(a for a in out["assumptions"]
                if a["field"] == "evacuation_priority_rule")
    assert "containers per trip" in rule["value"]


@pytest.mark.asyncio
async def test_driver_shortage_without_trips_reports_missing_data():
    out = await SimulationService(repository=FakeSimRepo()).run("driver-shortage", {
        "from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)})
    assert out["data_available"] is False


# ================================================================ contract + service
@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,params", [
    ("berth-cascade", {"terminal": "NSICT", "as_of": AUG2}),
    ("crane-productivity", {"as_of": AUG6}),
    ("modal-shift", {"from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)}),
    ("gate-slotting", {"from_ts": _ts(1, 0), "to_ts": _ts(2, 0)}),
    ("driver-shortage", {"from_date": date(2026, 8, 1), "to_date": date(2026, 8, 3)}),
])
async def test_every_scenario_returns_the_jnpa_contract(scenario, params):
    """Notice §1: every answer carries method, result+figures, assumptions
    (separately), and the queries behind it — even when the data is empty."""
    out = await SimulationService(repository=FakeSimRepo()).run(scenario, params)
    for key in ("scenario", "method", "result", "figures", "assumptions",
                "queries", "recommendations", "data_available", "notes"):
        assert key in out, f"{scenario} is missing '{key}'"
    assert out["scenario"] == scenario
    assert out["method"].strip()
    assert out["queries"], f"{scenario} published no query trace"
    for q in out["queries"]:
        assert "sql" in q and "params" in q


@pytest.mark.asyncio
async def test_unknown_scenario_raises():
    with pytest.raises(SimulationError):
        await SimulationService(repository=FakeSimRepo()).run("nope", {})


def test_catalog_covers_every_registered_scenario():
    assert {c["scenario"] for c in CATALOG} == set(REGISTRY)
    for entry in CATALOG:
        assert entry["jnpa_reference"] and entry["question"] and entry["reads"]


# ======================================================================= the router
@pytest.fixture()
def client():
    from gateway.main import app
    from gateway.routers import cargo_simulation as sim_router

    repo = FakeSimRepo(berth_queue=BERTH_CALLS, calls_with_moves=_moves_rows(),
                       gate_hourly_profile=GATE_PROFILE, rail_road_daily=RAIL_DAILY,
                       vehicle_trips=VEHICLE_TRIPS)
    service = SimulationService(repository=repo)
    app.dependency_overrides[sim_router.get_service] = lambda: service
    with TestClient(app) as c:
        c.sim_repo = repo
        yield c
    app.dependency_overrides.pop(sim_router.get_service, None)


def test_scenarios_catalog_endpoint(client):
    r = client.get("/api/cargo/simulate/scenarios")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(REGISTRY)
    assert "contract" in body and "assumptions" in body["contract"]


def test_berth_cascade_endpoint(client):
    r = client.post("/api/cargo/simulate/berth-cascade",
                    json={"terminal": "NSICT", "as_of": "2026-08-02T00:00:00Z",
                          "delay_hours": 6, "vessel_name": "VESSEL ONE"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["figures"]["cumulative_delay_hours"] == 12.0
    assert body["assumptions"] and body["queries"]


def test_crane_productivity_endpoint(client):
    r = client.post("/api/cargo/simulate/crane-productivity",
                    json={"as_of": "2026-08-06T00:00:00Z", "reduction_pct": 0.25,
                          "vessel_name": "VESSEL ONE"})
    assert r.status_code == 200, r.text
    assert r.json()["figures"]["baseline_moves_per_hour"] == 100.0


def test_modal_shift_endpoint(client):
    r = client.post("/api/cargo/simulate/modal-shift",
                    json={"from_date": "2026-08-01", "to_date": "2026-08-03",
                          "shift_pct": 0.2, "sustained_rate": 100})
    assert r.status_code == 200, r.text
    # The shared client fixture uses GATE_PROFILE (400 trips against 4,000 road
    # TEU = 10 TEU/trip), which the plausibility guard rejects, so the conversion
    # falls back to 1 TEU/trip: 160 shifted TEU -> 160 trips. The unguarded figure
    # was 16, which understated the road load by a factor of ten.
    assert r.json()["figures"]["additional_truck_trips"] == 160


def test_gate_slotting_endpoint(client):
    r = client.post("/api/cargo/simulate/gate-slotting",
                    json={"from_ts": "2026-08-01T00:00:00Z",
                          "to_ts": "2026-08-02T00:00:00Z", "sustained_rate": 100})
    assert r.status_code == 200, r.text
    assert r.json()["figures"]["observed_peak"] == 150


def test_driver_shortage_endpoint(client):
    r = client.post("/api/cargo/simulate/driver-shortage",
                    json={"from_date": "2026-08-01", "to_date": "2026-08-03",
                          "state_date": "2026-08-04"})
    assert r.status_code == 200, r.text
    assert r.json()["figures"]["trips_lost"] == 3


def test_generic_endpoint_coerces_iso_dates(client):
    r = client.post("/api/cargo/simulate/driver-shortage",
                    json={"from_date": "2026-08-01", "to_date": "2026-08-03"})
    assert r.status_code == 200, r.text


def test_unknown_scenario_is_400(client):
    r = client.post("/api/cargo/simulate/not-a-scenario", json={})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_scenario_request"


def test_invalid_window_is_422(client):
    """to_date before from_date is refused by the DTO validator."""
    r = client.post("/api/cargo/simulate/modal-shift",
                    json={"from_date": "2026-08-03", "to_date": "2026-08-01"})
    assert r.status_code in (400, 422)


def test_simulate_routes_do_not_shadow_the_container_lookup(client):
    """`/api/cargo/simulate/scenarios` must not be read as a container number by
    GET /api/cargo/{container_number} — the same route-ordering hazard the cargo
    router documents for /events."""
    r = client.get("/api/cargo/simulate/scenarios")
    assert r.status_code == 200
    assert "scenarios" in r.json()


def test_gate_hourly_profile_endpoint(client):
    r = client.get("/api/gate/hourly-profile",
                   params={"from": "2026-08-01T00:00:00Z", "to": "2026-08-02T00:00:00Z"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_arrivals"] == 400
    assert body["peak_arrivals"] == 150
    assert body["count"] == 6
    assert body["source"] == "core.eir"
    assert body["queries"]


def test_gate_hourly_profile_groups_by_day(client):
    r = client.get("/api/gate/hourly-profile",
                   params={"from": "2026-08-01T00:00:00Z", "to": "2026-08-02T00:00:00Z",
                           "group_by": "day"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["buckets"][0]["arrivals"] == 400
    assert body["notes"], "the daily unique-truck caveat must be stated"


def test_gate_hourly_profile_rejects_an_inverted_window(client):
    r = client.get("/api/gate/hourly-profile",
                   params={"from": "2026-08-02T00:00:00Z", "to": "2026-08-01T00:00:00Z"})
    assert r.status_code == 400


def test_gate_hourly_profile_rejects_an_oversized_window(client):
    r = client.get("/api/gate/hourly-profile",
                   params={"from": "2026-01-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"})
    assert r.status_code == 400


def test_simulation_endpoints_never_write(client):
    """End-to-end read-only proof: run every scenario through the router and assert
    the repository was only ever asked read questions."""
    client.post("/api/cargo/simulate/berth-cascade",
                json={"terminal": "NSICT", "as_of": "2026-08-02T00:00:00Z"})
    client.post("/api/cargo/simulate/driver-shortage",
                json={"from_date": "2026-08-01", "to_date": "2026-08-03"})
    read_methods = {"berth_queue", "calls_with_moves", "gate_hourly_profile",
                    "gate_event_hourly", "tas_hourly_capacity", "rail_road_daily",
                    "evacuation_mode_split", "vehicle_trips", "cargo_flows",
                    "pendency_snapshot"}
    assert client.sim_repo.calls, "no repository call was recorded"
    assert set(client.sim_repo.calls) <= read_methods
