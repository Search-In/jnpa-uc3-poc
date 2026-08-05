"""Business State Engine — ladder integrity, state derivation, and de-duplication.

Pure: no DB, no corpus. The engine is the ONLY place that understands call progression, so
these tests pin (a) the ladder, (b) each derived state, and (c) that repository.py no
longer restates any of it.
"""
from __future__ import annotations

import datetime as dt

import pytest

from services.marine import state_engine as SE
from services.marine.parsers.pcs_common import (CALL_STATUS_BERTH_ALLOTTED,
                                                CALL_STATUS_BERTH_PLANNED,
                                                CALL_STATUS_PLANNED,
                                                CALL_STATUS_VCN_ALLOTTED)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def ev(t: str, when: dt.datetime | None = None) -> dict:
    return {"event_type": t, "event_ts": when or dt.datetime(2026, 7, 29, 12, 0, tzinfo=IST)}


def state(*types: str, status: str | None = None):
    return SE.derive_state({"status": status}, [ev(t) for t in types])


# ---------------------------------------------------------------- ladder
class TestLadder:
    def test_event_order_is_the_documented_business_lifecycle(self):
        # The five between BERTH_ALLOTTED and DEPARTED that core.pilotage records —
        # memo lodged, first line, all fast, pilot away, berth cleared — were added when
        # the engine started consuming those columns. Placement is operational order:
        # lines ashore precede all fast, the pilot leaves after, the berth is cleared
        # before the vessel sails.
        # CRAFT_* sit with the pilot block because craft are committed for the duration
        # of a movement, and BELOW the mooring milestones on purpose: a tug standing down
        # must never outrank ALL_FAST or DEPARTED and mask the vessel's own progress.
        # The five CRAFT_* rungs sit ABOVE the mooring milestones: a craft movement is
        # routinely ordered against a vessel already alongside, so ranking them lower
        # would leave `latest_event` stuck on BERTHED after a dispatch. They stay BELOW
        # SAILED/DEPARTED, which end the call and must never be masked by a tug.
        assert SE.EVENT_ORDER == ("BERTH_ALLOTTED", "PILOT_REQUESTED", "ANCHORED",
                                  "ANCHOR_AWEIGH", "PILOT_BOARDED",
                                  "FIRST_LINE", "ALL_FAST", "BERTHED", "ARRIVED",
                                  "CRAFT_ASSIGNED", "CRAFT_DISPATCHED",
                                  "CRAFT_ON_SCENE", "CRAFT_ASSISTING",
                                  "CRAFT_RELEASED",
                                  "PILOT_DISEMBARKED", "BERTH_VACATED",
                                  "SAILED", "DEPARTED")

    def test_status_order_follows_the_message_flow(self):
        assert SE.STATUS_ORDER == (CALL_STATUS_PLANNED, CALL_STATUS_VCN_ALLOTTED,
                                   CALL_STATUS_BERTH_PLANNED, CALL_STATUS_BERTH_ALLOTTED)

    def test_ranks_increase_monotonically(self):
        ranks = [SE.event_rank(e) for e in SE.EVENT_ORDER]
        assert ranks == sorted(ranks) == list(range(len(SE.EVENT_ORDER)))

    def test_unknown_ranks_below_everything_not_at_zero(self):
        """-1, not 0 — otherwise an unrecognised milestone ties with BERTH_ALLOTTED and
        could be reported as the latest event."""
        assert SE.event_rank("NOT_A_MILESTONE") == -1
        assert SE.event_rank(None) == -1
        assert SE.status_rank("legacy free text") == -1
        assert SE.event_rank("BERTH_ALLOTTED") == 0

    def test_rank_lookup_is_case_and_space_tolerant(self):
        assert SE.event_rank("  berthed ") == SE.event_rank("BERTHED")


# ---------------------------------------------------------------- derivation
class TestDerivation:
    def test_no_events_keeps_the_parser_stage(self):
        s = SE.derive_state({"status": CALL_STATUS_BERTH_PLANNED}, [])
        assert s.status == CALL_STATUS_BERTH_PLANNED
        assert s.latest_event is None and s.latest_event_time is None
        assert (s.arrival_state, s.berth_state, s.pilot_state, s.departure_state) == (
            "Pending", "Pending", "Pending", "Pending")
        assert s.is_in_port is False and s.is_at_berth is False

    def test_berth_allotted(self):
        s = state("BERTH_ALLOTTED", status=CALL_STATUS_BERTH_ALLOTTED)
        assert s.status == CALL_STATUS_BERTH_ALLOTTED
        assert s.berth_state == "Allotted"
        assert s.is_at_berth is False

    def test_anchored(self):
        s = state("BERTH_ALLOTTED", "ANCHORED")
        assert s.status == SE.STATUS_ANCHORED
        assert s.arrival_state == "Anchored"
        assert s.is_in_port is False

    def test_pilot_boarded_makes_the_pilot_active_and_craft_busy(self):
        s = state("ANCHORED", "PILOT_BOARDED")
        assert s.status == SE.STATUS_PILOT_BOARDED
        assert s.pilot_state == "Active"
        assert s.portcraft_state == "Busy"

    def test_the_specified_at_berth_example_reproduces_exactly(self):
        """The worked example from the specification."""
        s = state("BERTH_ALLOTTED", "ANCHORED", "PILOT_BOARDED", "BERTHED")
        assert s.status == "At Berth"
        assert s.arrival_state == "Completed"
        assert s.berth_state == "Occupied"
        assert s.pilot_state == "Completed"
        assert s.departure_state == "Pending"
        assert s.shipping_state == "In Port"
        assert s.portcraft_state == "Busy"
        assert s.latest_event == "BERTHED"
        assert s.is_in_port is True and s.is_at_berth is True

    def test_arrived_sets_in_port(self):
        s = state("ARRIVED")
        assert s.is_in_port is True
        assert s.arrival_state == "Completed"
        assert s.shipping_state == "In Port"

    def test_sailed_is_departing_but_still_in_port(self):
        s = state("BERTHED", "ARRIVED", "SAILED")
        assert s.status == SE.STATUS_SAILING
        assert s.departure_state == "Sailing"
        assert s.is_in_port is True

    def test_departed_clears_everything(self):
        s = state("BERTHED", "ARRIVED", "SAILED", "DEPARTED")
        assert s.status == SE.STATUS_DEPARTED
        assert s.departure_state == "Completed"
        assert s.berth_state == "Released"
        assert s.shipping_state == "Sailed"
        assert s.portcraft_state == "Idle"
        assert s.is_in_port is False and s.is_at_berth is False

    def test_berthed_completes_arrival_without_an_arrived_milestone(self):
        """A berthed vessel has necessarily arrived. The two share a timestamp in the
        corpus, so neither may be assumed present."""
        s = state("BERTHED")
        assert s.arrival_state == "Completed"
        assert s.is_in_port is True

    def test_unknown_milestone_never_advances_state(self):
        s = SE.derive_state({"status": CALL_STATUS_PLANNED}, [ev("TEA_BREAK")])
        assert s.status == CALL_STATUS_PLANNED
        assert s.latest_event is None


class TestOrderingIsByRankNotClock:
    """The corpus ties ARRIVED with BERTHED and puts SAILED BEFORE DEPARTED, so a
    clock-ordered 'latest' is both non-deterministic and wrong."""

    def test_tied_timestamps_resolve_by_rank(self):
        same = dt.datetime(2026, 7, 29, 21, 24, tzinfo=IST)
        s = SE.derive_state({}, [ev("BERTHED", same), ev("ARRIVED", same)])
        assert s.latest_event == "ARRIVED"      # higher rank wins the tie

    def test_sailed_before_departed_by_clock_still_reports_departed(self):
        s = SE.derive_state({}, [
            ev("SAILED", dt.datetime(2026, 7, 15, 2, 15, tzinfo=IST)),
            ev("DEPARTED", dt.datetime(2026, 7, 15, 4, 40, tzinfo=IST))])
        assert s.latest_event == "DEPARTED"
        assert s.departure_state == "Completed"

    def test_events_may_arrive_in_any_order(self):
        forward = state("ANCHORED", "PILOT_BOARDED", "BERTHED")
        reverse = state("BERTHED", "PILOT_BOARDED", "ANCHORED")
        assert forward == reverse

    def test_duplicate_milestones_are_idempotent(self):
        """VESDEP is emitted twice for one call in the corpus."""
        once = state("SAILED", "DEPARTED")
        twice = state("SAILED", "DEPARTED", "SAILED", "DEPARTED")
        assert once == twice

    def test_a_missing_timestamp_never_outranks_a_timed_row(self):
        s = SE.derive_state({}, [
            {"event_type": "DEPARTED", "event_ts": None},
            ev("DEPARTED", dt.datetime(2026, 7, 15, 4, 40, tzinfo=IST))])
        assert s.latest_event_time is not None


# ---------------------------------------------------------------- de-duplication
class TestSingleSourceOfTruth:
    def test_repository_imports_the_ladder_rather_than_restating_it(self):
        from services.marine import repository as R
        assert R._STATUS_ORDER == SE.status_order_sql()

    def test_status_ladder_sql_matches_the_python_ladder(self):
        sql = SE.status_order_sql()
        for s in SE.STATUS_ORDER:
            assert f"'{s}'" in sql
        assert sql.startswith("ARRAY[") and sql.endswith("]")

    def test_in_port_predicate_is_defined_once(self):
        src = open("services/marine/repository.py", encoding="utf-8").read()
        assert "c.ata IS NOT NULL AND c.atd IS NULL" not in src, \
            "in_port re-stated inline; use state_engine.in_port_sql()"
        assert src.count("in_port_sql(") >= 3

    def test_in_port_sql_is_alias_parameterised(self):
        assert SE.in_port_sql("c") == "c.ata IS NOT NULL AND c.atd IS NULL"
        assert SE.in_port_sql("x").startswith("x.")

    def test_projection_uses_engine_event_constants(self):
        from services.marine import repository as R
        assert f"'{SE.EVENT_ARRIVED}'" in R._PROJECT_CALL_ACTUALS
        assert f"'{SE.EVENT_DEPARTED}'" in R._PROJECT_CALL_ACTUALS

    def test_engine_is_pure_no_database_imports(self):
        """It must stay unit-testable and unable to change query behaviour by accident."""
        src = open("services/marine/state_engine.py", encoding="utf-8").read()
        for banned in ("get_engine", "sqlalchemy", "asyncpg", "await "):
            assert banned not in src, f"state_engine must stay pure: found {banned!r}"


class TestReturnModel:
    def test_every_specified_field_is_present(self):
        d = state("BERTHED").to_dict()
        # craft_state joined the model when the CRAFT_* milestones entered the shared
        # ledger — supply (are craft committed) beside portcraft_state's demand.
        assert set(d) == {"craft_state",
                          "status", "arrival_state", "berth_state", "pilot_state",
                          "departure_state", "shipping_state", "portcraft_state",
                          "is_in_port", "is_at_berth", "latest_event", "latest_event_time"}

    def test_state_is_immutable(self):
        s = state("BERTHED")
        with pytest.raises(Exception):
            s.status = "tampered"  # type: ignore[misc]
