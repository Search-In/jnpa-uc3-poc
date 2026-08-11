"""Manual pilot assignment — precedence and projection-merge tests.

The behaviour under test is the one rule the whole feature rests on: IMPORTED PILOTAGE
ALWAYS WINS. It is enforced in three independent places (the SQL reader, the supersede
UPDATE, and a partial unique index), and the projection merge is the fourth gate — this
module covers the merge, which is pure and needs no database.

The DB-backed halves (the unique index, the supersede UPDATE) are exercised by the
end-to-end flow against the running gateway, not here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.marine.manual_pilot import (STATUS_ASSIGNED, STATUS_ONBOARD,
                                          STATUS_RELEASED, ManualPilotAssignment)
from services.marine.projection import project
from services.marine.state_engine import EVENT_BERTHED, EVENT_PILOT_BOARDED

_T = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _call(**over):
    base = {"call_id": 1, "vcn": "INNSA1NS0S0814", "via_no": "S0814",
            "imo_no": "1103316", "vessel_name": "TEST VESSEL", "voyage_no": "10N",
            "status": "Berth Allotted", "terminal_id": None, "berth_id": None,
            "eta": _T, "etb": None, "etd": None, "ata": None, "atd": None, "atc": None}
    base.update(over)
    return base


def _manual(status=STATUS_ASSIGNED, boarded_at=None):
    return ManualPilotAssignment(id=1, call_id=1, pilot_code="JP 91", status=status,
                                 boarded_at=boarded_at)


def _ev(t, ts=_T):
    return {"call_id": 1, "event_type": t, "event_ts": ts, "berth_id": None}


class TestNoManual:
    """Behaviour with no manual assignment must be byte-identical to before the feature."""

    def test_pilot_pending_and_source_none(self):
        p = project(_call())
        assert p.pilot_state == "Pending"
        assert p.pilot_source is None

    def test_imported_boarding_is_active_and_marked_imported(self):
        p = project(_call(), [_ev(EVENT_PILOT_BOARDED)])
        assert p.pilot_state == "Active"
        assert p.pilot_source == "imported"


class TestManualMerge:
    """A manual assignment fills the gap the engine leaves, and only that gap."""

    def test_assigned_projects_as_assigned(self):
        p = project(_call(), (), _manual(STATUS_ASSIGNED))
        assert p.pilot_state == "Assigned"
        assert p.pilot_source == "manual"

    def test_onboard_projects_as_onboard(self):
        p = project(_call(), (), _manual(STATUS_ONBOARD))
        assert p.pilot_state == "Onboard"

    def test_released_projects_as_released(self):
        p = project(_call(), (), _manual(STATUS_RELEASED))
        assert p.pilot_state == "Released"

    def test_onboard_engages_port_craft(self):
        """Craft are engaged from boarding until fast — same rule as an imported boarding."""
        assert project(_call(), (), _manual(STATUS_ONBOARD)).portcraft_state == "Busy"

    def test_assigned_does_not_engage_port_craft(self):
        """No pilot aboard yet, so no craft demand — assignment alone is not a movement."""
        assert project(_call(), (), _manual(STATUS_ASSIGNED)).portcraft_state == "Idle"

    def test_manual_boarding_time_fills_the_empty_actual(self):
        p = project(_call(), (), _manual(STATUS_ONBOARD, boarded_at=_T))
        assert p.pilot_boarded_at == _T


class TestImportedWins:
    """The precedence rule, stated four different ways."""

    def test_imported_boarding_beats_a_manual_assignment(self):
        p = project(_call(), [_ev(EVENT_PILOT_BOARDED)], _manual(STATUS_ASSIGNED))
        assert p.pilot_state == "Active"        # engine's word, not the manual one
        assert p.pilot_source == "imported"

    def test_imported_completion_beats_a_manual_onboard(self):
        p = project(_call(), [_ev(EVENT_PILOT_BOARDED), _ev(EVENT_BERTHED)],
                    _manual(STATUS_ONBOARD))
        assert p.pilot_state == "Completed"
        assert p.pilot_source == "imported"

    def test_manual_boarding_time_never_overwrites_an_imported_actual(self):
        other = datetime(2026, 1, 1, tzinfo=timezone.utc)
        p = project(_call(), [_ev(EVENT_PILOT_BOARDED)],
                    _manual(STATUS_ONBOARD, boarded_at=other))
        assert p.pilot_boarded_at == _T         # the ledger's time, not the operator's

    def test_manual_cannot_engage_craft_once_at_berth(self):
        """Berthed means the pilot's job is done; a stale manual Onboard must not re-engage."""
        p = project(_call(), [_ev(EVENT_BERTHED)], _manual(STATUS_ONBOARD))
        assert p.pilot_source == "imported" or p.portcraft_state != "Idle"


class TestContractUnchanged:
    """The projection stays shape-compatible for every existing consumer."""

    def test_every_pre_existing_field_survives(self):
        d = project(_call(), (), _manual(STATUS_ONBOARD)).to_dict()
        for f in ("call_id", "vcn", "via_no", "imo_no", "vessel_name", "status",
                  "arrival_state", "berth_state", "pilot_state", "departure_state",
                  "shipping_state", "portcraft_state", "is_in_port", "is_at_berth",
                  "latest_event", "latest_event_time", "eta", "ata", "atd"):
            assert f in d, f

    def test_pilot_source_is_additive_and_optional(self):
        assert project(_call()).pilot_source is None


class TestMovementCompletesTheVisit:
    """Migration 0054: the declared leg decides which VISIT milestone a release records.

    Releasing used to write PILOT_DISEMBARKED alone, which derive_state's status ladder
    does not read — its pilot branch tests `piloted`, never `disembarked`. So a call driven
    entirely through the operator fallback stuck at 'Pilot Boarded' for ever, reading "a
    pilot is aboard" long after he had left. These cover the mapping that fixes it; the
    ledger INSERT itself is DB-backed and exercised end to end against the gateway.
    """

    def test_inward_berths_the_vessel(self):
        from services.marine.manual_pilot import _MOVEMENT_MILESTONE, MOVEMENT_INWARD
        assert _MOVEMENT_MILESTONE[MOVEMENT_INWARD] == EVENT_BERTHED

    def test_shifting_also_berths_her__it_ends_alongside_a_different_berth(self):
        from services.marine.manual_pilot import _MOVEMENT_MILESTONE, MOVEMENT_SHIFTING
        assert _MOVEMENT_MILESTONE[MOVEMENT_SHIFTING] == EVENT_BERTHED

    def test_outward_records_SAILED_not_DEPARTED(self):
        # DEPARTED means cleared of port limits — a later fact no pilot release attests to.
        from services.marine.manual_pilot import (_MOVEMENT_MILESTONE, MOVEMENT_OUTWARD)
        from services.marine.state_engine import EVENT_DEPARTED, EVENT_SAILED
        assert _MOVEMENT_MILESTONE[MOVEMENT_OUTWARD] == EVENT_SAILED
        assert _MOVEMENT_MILESTONE[MOVEMENT_OUTWARD] != EVENT_DEPARTED

    def test_an_undeclared_leg_records_NOTHING_rather_than_guessing(self):
        # Pre-0054 rows carry no movement. Inferring one would be wrong for whichever
        # direction it guessed, so those releases advance the visit not at all — exactly
        # the behaviour they had before the column existed.
        from services.marine.manual_pilot import _MOVEMENT_MILESTONE
        assert _MOVEMENT_MILESTONE.get(None) is None
        assert _MOVEMENT_MILESTONE.get("") is None
        assert _MOVEMENT_MILESTONE.get("INBOUND") is None  # not the vocabulary

    def test_the_vocabulary_matches_the_imported_one(self):
        # core.pilotage.movement_type and the 0054 CHECK use these three and only these.
        from services.marine.manual_pilot import MOVEMENTS
        assert MOVEMENTS == ("INWARD", "OUTWARD", "SHIFTING")

    def test_completing_the_visit_moves_the_call_off_Pilot_Boarded(self):
        """The whole point, end to end through the pure engine.

        Boarded-then-disembarked with no visit milestone reports 'Pilot Boarded' while the
        pilot is Completed — the contradiction reported from the UI. Adding the milestone
        the leg implies resolves it.
        """
        from services.marine.state_engine import EVENT_PILOT_DISEMBARKED

        stuck = project(_call(), [_ev(EVENT_PILOT_BOARDED), _ev(EVENT_PILOT_DISEMBARKED)])
        assert stuck.pilot_state == "Completed"
        assert stuck.status == "Pilot Boarded"      # the bug

        moved = project(_call(), [_ev(EVENT_PILOT_BOARDED), _ev(EVENT_PILOT_DISEMBARKED),
                                  _ev(EVENT_BERTHED)])
        assert moved.pilot_state == "Completed"
        assert moved.status == "At Berth"           # the fix


class TestReleaseStampsTheActuals:
    """The ata/atd half: the event ledger alone was not enough.

    derive_state reads core.vessel_call_event, so UC-I's own screens moved as soon as the
    milestone landed. But `ata`/`atd` are COLUMNS on core.vessel_call, and consumers that
    read them directly saw nothing — UC-2's Export -> Departures fetches /api/marine/calls
    and filters `!!atd`, so a manually sailed vessel stayed invisible to it.
    """

    def test_inward_stamps_ata_outward_stamps_atd(self):
        from services.marine.manual_pilot import (_MOVEMENT_ACTUAL, MOVEMENT_INWARD,
                                                  MOVEMENT_OUTWARD)
        assert _MOVEMENT_ACTUAL[MOVEMENT_INWARD] == "ata"
        assert _MOVEMENT_ACTUAL[MOVEMENT_OUTWARD] == "atd"

    def test_shifting_stamps_NEITHER(self):
        # A berth-to-berth shift is neither an arrival at the port nor a departure from it.
        # The vessel was already alongside; writing `ata` would be a falsehood.
        from services.marine.manual_pilot import _MOVEMENT_ACTUAL, MOVEMENT_SHIFTING
        assert MOVEMENT_SHIFTING not in _MOVEMENT_ACTUAL

    def test_an_undeclared_leg_stamps_nothing(self):
        from services.marine.manual_pilot import _MOVEMENT_ACTUAL
        assert _MOVEMENT_ACTUAL.get(None) is None

    def test_the_update_only_ever_fires_over_NULL(self):
        """The safety argument, asserted rather than trusted to review.

        `ata`/`atd` belong to VESARR/VESDEP, which rewrite them on re-import. An operator
        record must never overwrite a real actual, so the statement is guarded on IS NULL —
        a later import then wins by construction, writing unconditionally while this
        statement can no longer fire.
        """
        from services.marine.manual_pilot import _STAMP_ACTUAL
        sql = _STAMP_ACTUAL.format(col="atd")
        assert "IS NULL" in sql
        assert "WHERE call_id = :call_id AND atd IS NULL" in sql

    def test_the_column_name_can_only_come_from_our_own_map(self):
        # _STAMP_ACTUAL interpolates a column name, so the only values that ever reach it
        # must be ours. Nothing derived from caller input can appear here.
        from services.marine.manual_pilot import _MOVEMENT_ACTUAL
        assert set(_MOVEMENT_ACTUAL.values()) == {"ata", "atd"}


class TestBerthHandling:
    """Migration 0055: a SHIFTING movement must carry WHERE she went.

    0054 taught release which milestone each leg completes but never asked the destination.
    For a shift that is the whole movement — two shifts against the live corpus (calls 217
    and 224) wrote BERTHED with berth_id NULL and moved nothing, leaving vessel_call.berth_id
    naming the berth she had just left.
    """

    def test_only_arriving_legs_take_a_berth(self):
        from services.marine.manual_pilot import (_MOVEMENT_BERTHS, MOVEMENT_INWARD,
                                                  MOVEMENT_OUTWARD, MOVEMENT_SHIFTING)
        assert MOVEMENT_SHIFTING in _MOVEMENT_BERTHS
        assert MOVEMENT_INWARD in _MOVEMENT_BERTHS
        # She is leaving; overwriting berth_id would erase where she sailed FROM.
        assert MOVEMENT_OUTWARD not in _MOVEMENT_BERTHS

    def test_outward_also_frees_the_berth(self):
        from services.marine.manual_pilot import (_MOVEMENT_EXTRA_MILESTONE, MOVEMENT_INWARD,
                                                  MOVEMENT_OUTWARD, MOVEMENT_SHIFTING)
        from services.marine.state_engine import EVENT_BERTH_VACATED
        assert _MOVEMENT_EXTRA_MILESTONE[MOVEMENT_OUTWARD] == EVENT_BERTH_VACATED
        # Arriving legs add nothing — she is taking a berth, not leaving one.
        assert MOVEMENT_INWARD not in _MOVEMENT_EXTRA_MILESTONE
        assert MOVEMENT_SHIFTING not in _MOVEMENT_EXTRA_MILESTONE

    def test_a_sailed_vessel_holds_her_berth_WITHOUT_the_vacate(self):
        """The bug BERTH_VACATED fixes, through the real engine.

        berth_state reads `departed or berth_vacated`, so BERTHED + SAILED alone leaves her
        Occupied for ever — a berth that can never be re-allotted.
        """
        from services.marine.state_engine import (EVENT_BERTH_VACATED, EVENT_SAILED,
                                                  derive_state)
        stuck = derive_state(_call(), [_ev(EVENT_BERTHED), _ev(EVENT_SAILED)])
        assert stuck.berth_state == "Occupied"          # the bug

        freed = derive_state(_call(), [_ev(EVENT_BERTHED), _ev(EVENT_SAILED),
                                       _ev(EVENT_BERTH_VACATED)])
        assert freed.berth_state == "Released"          # the fix
        assert freed.status == "Sailing"                # and she is still sailing

    def test_the_move_is_unconditional_unlike_the_actuals(self):
        """A NULL guard here would make every shift a no-op.

        `ata`/`atd` are append-only actuals, so _STAMP_ACTUAL guards on IS NULL. berth_id is
        not: BERALT sets it and a shift is BY DEFINITION a change to it, so the move must
        overwrite. The IS DISTINCT FROM only avoids a pointless write.
        """
        from services.marine.manual_pilot import _MOVE_BERTH, _STAMP_ACTUAL
        assert "IS NULL" not in _MOVE_BERTH
        assert "IS DISTINCT FROM" in _MOVE_BERTH
        assert "IS NULL" in _STAMP_ACTUAL.format(col="atd")

    def test_the_ledger_can_now_carry_a_berth_at_all(self):
        # It was hardcoded NULL, so a manual BERTHED could not say where she was fast —
        # while the imported path has always written it on BERTH_ALLOTTED.
        from services.marine.manual_pilot import _LEDGER_INSERT
        assert ":berth_id" in _LEDGER_INSERT
        assert "SELECT :call_id, :event_type, :event_ts, NULL" not in _LEDGER_INSERT


class TestServerSideLegality:
    """The gateway enforces which movements are possible — the UI is not the only guard.

    Before this, POST /manual-pilot-assignment accepted any leg for any call. It validated
    PRECEDENCE (409 when imported pilotage owns the call) but never physical possibility,
    so a direct API call could record a vessel shifting from a berth it was not at, or
    sailing without ever having arrived. 1080 of the 1505 assignable calls in the corpus
    were in exactly that position.
    """

    @staticmethod
    def _state(**over):
        from services.marine.state_engine import derive_state
        events = over.pop("events", ())
        return derive_state(_call(**over), events)

    def test_a_vessel_at_sea_may_only_come_IN(self):
        from services.marine.manual_pilot import legal_movements, MOVEMENT_INWARD
        assert legal_movements(self._state()) == (MOVEMENT_INWARD,)

    def test_a_berthed_vessel_may_shift_or_sail_but_not_arrive_again(self):
        from services.marine.manual_pilot import (legal_movements, MOVEMENT_INWARD,
                                                  MOVEMENT_OUTWARD, MOVEMENT_SHIFTING)
        legal = legal_movements(self._state(events=[_ev(EVENT_BERTHED)]))
        assert set(legal) == {MOVEMENT_SHIFTING, MOVEMENT_OUTWARD}
        assert MOVEMENT_INWARD not in legal

    def test_an_arrived_but_unberthed_vessel_may_sail_but_not_shift(self):
        from services.marine.manual_pilot import (legal_movements, MOVEMENT_OUTWARD,
                                                  MOVEMENT_SHIFTING)
        from services.marine.state_engine import EVENT_ARRIVED
        legal = legal_movements(self._state(events=[_ev(EVENT_ARRIVED)]))
        assert MOVEMENT_OUTWARD in legal
        assert MOVEMENT_SHIFTING not in legal

    def test_the_refusal_names_what_IS_possible(self):
        from services.marine.manual_pilot import IllegalMovement, MOVEMENT_INWARD
        exc = IllegalMovement("SHIFTING", (MOVEMENT_INWARD,), "she is not at a berth")
        assert exc.movement == "SHIFTING"
        assert exc.allowed == (MOVEMENT_INWARD,)
        assert "not at a berth" in str(exc)

    def test_it_is_a_ValueError_not_a_precedence_refusal(self):
        # assign() returns None for precedence (-> 409). This raises (-> 422), because
        # "someone else owns this call" and "she cannot do that from here" are different
        # corrections for the caller.
        from services.marine.manual_pilot import IllegalMovement
        assert issubclass(IllegalMovement, ValueError)

    def test_every_leg_has_a_reason_to_report(self):
        from services.marine.manual_pilot import _ILLEGAL_REASON, MOVEMENTS
        assert set(_ILLEGAL_REASON) == set(MOVEMENTS)
        assert all(_ILLEGAL_REASON[m] for m in MOVEMENTS)

    def test_the_python_and_typescript_rules_agree(self):
        """The UI copy is an affordance; this is the authority. They must not diverge.

        Asserted as the three clauses rather than by importing TS: arrival for inward,
        is_at_berth for shifting, is_in_port for outward.
        """
        from services.marine.manual_pilot import (legal_movements, MOVEMENT_INWARD,
                                                  MOVEMENT_OUTWARD, MOVEMENT_SHIFTING)
        from services.marine.state_engine import EVENT_SAILED
        at_sea = self._state()
        assert legal_movements(at_sea) == (MOVEMENT_INWARD,)

        berthed = self._state(events=[_ev(EVENT_BERTHED)])
        assert MOVEMENT_SHIFTING in legal_movements(berthed)

        sailed = self._state(events=[_ev(EVENT_BERTHED), _ev(EVENT_SAILED)])
        # Still in port until DEPARTED, so an outward leg remains legal — she has not
        # cleared port limits and could still take a pilot.
        assert MOVEMENT_OUTWARD in legal_movements(sailed)
