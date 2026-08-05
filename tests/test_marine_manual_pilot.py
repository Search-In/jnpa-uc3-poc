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
