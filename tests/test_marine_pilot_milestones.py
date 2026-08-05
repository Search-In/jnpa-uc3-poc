"""Phase 1 — pilot-movement milestones become lifecycle events.

core.pilotage records seven milestones per movement; the engine consumed one. These tests
cover the synthesis, the imported-wins merge, and the states the new milestones unlock.
Pure: no DB, no corpus.
"""
from __future__ import annotations

import datetime as dt

from services.marine.pilot_milestones import SOURCE_PILOTAGE, merge_events, synthesize
from services.marine.projection import project

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def t(hour: int) -> dt.datetime:
    return dt.datetime(2026, 7, 29, hour, 0, tzinfo=IST)


def row(**over):
    base = {"call_id": 1, "movement_type": "INWARD", "submitted_at": None,
            "anchor_down_at": None, "anchor_up_at": None, "pilot_boarded_at": None,
            "first_line_at": None, "all_fast_at": None,
            "pilot_disembarked_at": None, "berth_vacated_at": None}
    base.update(over)
    return base


def ev(kind, hour):
    return {"event_type": kind, "event_ts": t(hour)}


class TestSynthesis:
    def test_each_recorded_timestamp_becomes_one_milestone(self):
        out = synthesize([row(submitted_at=t(1), pilot_boarded_at=t(2),
                              first_line_at=t(3), all_fast_at=t(4))])
        assert [e["event_type"] for e in out] == [
            "PILOT_REQUESTED", "PILOT_BOARDED", "FIRST_LINE", "ALL_FAST"]

    def test_null_columns_produce_nothing(self):
        assert synthesize([row()]) == []

    def test_every_synthesised_event_is_marked_as_such(self):
        out = synthesize([row(all_fast_at=t(4))])
        assert out[0]["source"] == SOURCE_PILOTAGE

    def test_the_movement_is_carried_so_a_shift_is_distinguishable(self):
        out = synthesize([row(movement_type="SHIFTING", all_fast_at=t(4))])
        assert out[0]["movement_type"] == "SHIFTING"

    def test_a_movement_with_no_call_is_skipped(self):
        """An unlinked pilot card belongs to no call's timeline."""
        assert synthesize([row(call_id=None, all_fast_at=t(4))]) == []

    def test_two_movements_both_contribute(self):
        out = synthesize([row(pilot_boarded_at=t(2)),
                          row(movement_type="OUTWARD", pilot_boarded_at=t(9))])
        assert len(out) == 2

    def test_same_milestone_at_the_same_instant_collapses(self):
        """A milestone is a fact about the CALL, not the row that recorded it."""
        out = synthesize([row(all_fast_at=t(4)), row(all_fast_at=t(4))])
        assert len(out) == 1


class TestImportedWins:
    def test_a_ledger_milestone_suppresses_its_synthesised_twin(self):
        merged = merge_events([ev("PILOT_BOARDED", 2)],
                              synthesize([row(pilot_boarded_at=t(2))]))
        assert len(merged) == 1
        assert merged[0].get("source") is None       # the ledger row survived

    def test_a_differing_time_is_kept_as_a_real_discrepancy(self):
        """A pilot card timing a boarding an hour after VESARR is a second data point;
        collapsing them would silently hide a disagreement the operator should see."""
        merged = merge_events([ev("PILOT_BOARDED", 2)],
                              synthesize([row(pilot_boarded_at=t(3))]))
        assert len(merged) == 2

    def test_milestones_the_ledger_lacks_are_added(self):
        merged = merge_events([ev("PILOT_BOARDED", 2)],
                              synthesize([row(all_fast_at=t(4))]))
        assert {e["event_type"] for e in merged} == {"PILOT_BOARDED", "ALL_FAST"}

    def test_an_empty_ledger_still_yields_the_movement(self):
        assert len(merge_events([], synthesize([row(all_fast_at=t(4))]))) == 1


class TestStatesTheNewMilestonesUnlock:
    def _project(self, *events):
        return project({"call_id": 1, "status": "Berth Allotted"}, list(events))

    def test_first_line_puts_the_berth_into_mooring(self):
        p = self._project(ev("PILOT_BOARDED", 2), ev("FIRST_LINE", 3))
        assert p.berth_state == "Mooring"

    def test_all_fast_occupies_the_berth_without_a_berthed_message(self):
        """ALL_FAST means moored — the corpus records it on 116 movements and the engine
        previously ignored every one."""
        p = self._project(ev("PILOT_BOARDED", 2), ev("ALL_FAST", 4))
        assert p.berth_state == "Occupied"

    def test_all_fast_completes_the_pilot_job(self):
        p = self._project(ev("PILOT_BOARDED", 2), ev("ALL_FAST", 4))
        assert p.pilot_state == "Completed"

    def test_an_explicit_disembark_completes_the_pilot_job(self):
        p = self._project(ev("PILOT_BOARDED", 2), ev("PILOT_DISEMBARKED", 5))
        assert p.pilot_state == "Completed"

    def test_berth_vacated_releases_the_berth_before_departure(self):
        p = self._project(ev("PILOT_BOARDED", 2), ev("BERTH_VACATED", 6))
        assert p.berth_state == "Released"

    def test_latest_event_reflects_the_finest_milestone_reached(self):
        p = self._project(ev("PILOT_BOARDED", 2), ev("FIRST_LINE", 3), ev("ALL_FAST", 4))
        assert p.latest_event == "ALL_FAST"

    def test_the_new_mooring_milestones_do_not_promote_the_call_status(self):
        """FIRST_LINE and ALL_FAST refine berth_state only — they must NOT advance the
        coarse call status to 'At Berth', which stays owned by the BERTHED/ARRIVED
        messages. Promoting it here would move the Vessel Calls chip for every moored
        call in one step, which is a separate decision.

        NOTE this does not mean the status distribution is unchanged overall: a
        SYNTHESISED PILOT_BOARDED (from pilotage.pilot_boarded_at, present on all 423
        movements) does advance a call from its parser stage to 'Pilot Boarded' — that is
        the engine correctly consuming a boarding it previously ignored, and it moves ~81
        calls. Measured, expected, and reported."""
        p = self._project(ev("PILOT_BOARDED", 2), ev("ALL_FAST", 4))
        assert p.status == "Pilot Boarded"

    def test_a_call_with_no_pilot_milestones_is_untouched(self):
        p = self._project(ev("BERTH_ALLOTTED", 1))
        assert (p.berth_state, p.pilot_state) == ("Allotted", "Pending")


class TestPilotCompletionIsOrderAware:
    """A berthing completes the pilot's job only if it FOLLOWED the boarding.

    Membership alone declared the job finished the instant a pilot stepped aboard a vessel
    that was already alongside — a departure or shift pilot was reported Completed before
    starting. 18 calls in the client corpus record a boarding after their berthing.
    """

    def _p(self, *events):
        return project({"call_id": 1, "status": "Berth Allotted"}, list(events))

    def test_inbound_still_completes(self):
        """The case the rule exists for: board, then berth."""
        assert self._p(ev("PILOT_BOARDED", 10), ev("BERTHED", 12)).pilot_state == "Completed"

    def test_a_tie_still_completes(self):
        """The corpus routinely stamps milestones at the same instant."""
        assert self._p(ev("PILOT_BOARDED", 12), ev("BERTHED", 12)).pilot_state == "Completed"

    def test_boarding_after_berthing_is_still_working(self):
        """A departure or shift pilot on a berthed vessel has not finished."""
        assert self._p(ev("BERTHED", 8), ev("PILOT_BOARDED", 14)).pilot_state == "Active"

    def test_that_movement_completes_on_disembark(self):
        assert self._p(ev("BERTHED", 8), ev("PILOT_BOARDED", 14),
                       ev("PILOT_DISEMBARKED", 16)).pilot_state == "Completed"

    def test_all_fast_after_boarding_completes(self):
        assert self._p(ev("PILOT_BOARDED", 10), ev("ALL_FAST", 11)).pilot_state == "Completed"

    def test_all_fast_before_boarding_does_not(self):
        assert self._p(ev("ALL_FAST", 8), ev("PILOT_BOARDED", 14)).pilot_state == "Active"

    def test_departure_completes_regardless_of_order(self):
        """A sailed vessel's pilot is done however the milestones are stamped."""
        assert self._p(ev("BERTHED", 8), ev("PILOT_BOARDED", 14),
                       ev("DEPARTED", 20)).pilot_state == "Completed"

    def test_untimed_rows_fall_back_to_membership(self):
        p = project({"call_id": 1}, [{"event_type": "PILOT_BOARDED", "event_ts": None},
                                     {"event_type": "BERTHED", "event_ts": None}])
        assert p.pilot_state == "Completed"
