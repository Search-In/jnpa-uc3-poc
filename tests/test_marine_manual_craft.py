"""Phase 4 — manual craft assignment merged into the ONE projection.

Covers the projection merge and the dispatch ladder's shape. The DB-backed halves (the
partial unique index, the advance-only UPDATE) are exercised end-to-end against the
running gateway, not here.
"""
from __future__ import annotations

from datetime import datetime, timezone

from services.marine.manual_craft import (LADDER, STATUS_ASSIGNED, STATUS_ASSISTING,
                                          STATUS_DISPATCHED, STATUS_ON_SCENE,
                                          STATUS_RELEASED, ManualCraftAssignment)
from services.marine.projection import project

_T = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def _call(**over):
    base = {"call_id": 1, "status": "Berth Allotted", "eta": _T}
    base.update(over)
    return base


def _craft(status=STATUS_ASSIGNED, craft_id=7):
    return ManualCraftAssignment(id=craft_id, call_id=1, craft_id=craft_id,
                                 status=status, craft_name="Daisy Star",
                                 craft_type="Tug")


class TestLadder:
    def test_the_dispatch_ladder_is_the_documented_order(self):
        assert LADDER == ("Assigned", "Dispatched", "On Scene", "Assisting", "Released")

    def test_released_is_terminal(self):
        assert LADDER[-1] == STATUS_RELEASED


def _ev(kind):
    return {"event_type": kind, "event_ts": _T}


class TestCraftStateComesFromTheLedger:
    """craft_state is state_engine's, derived from CRAFT_ASSIGNED / CRAFT_RELEASED.

    The assignment ROWS never decide it — that duplication was removed when the craft
    milestones entered the shared ledger. Passing rows without events must therefore leave
    the state Idle, which is what these assert.
    """

    def test_no_events_is_idle(self):
        assert project(_call()).craft_state == "Idle"

    def test_the_ledger_commits_the_call(self):
        assert project(_call(), [_ev("CRAFT_ASSIGNED")]).craft_state == "Committed"

    def test_every_rung_has_its_own_state(self):
        """Each transition is an immutable event, and the state follows the highest rung
        reached — with several craft on one movement that is the furthest any has got."""
        chain, expect = [], {"CRAFT_DISPATCHED": "Dispatched",
                             "CRAFT_ON_SCENE": "OnScene",
                             "CRAFT_ASSISTING": "Assisting"}
        chain.append(_ev("CRAFT_ASSIGNED"))
        for kind, state in expect.items():
            chain.append(_ev(kind))
            assert project(_call(), list(chain)).craft_state == state, kind

    def test_latest_event_follows_the_newest_rung(self):
        p = project(_call(), [_ev("BERTHED"), _ev("CRAFT_ASSIGNED"),
                              _ev("CRAFT_DISPATCHED")])
        assert p.latest_event == "CRAFT_DISPATCHED"

    def test_departure_still_outranks_a_craft_standing_down(self):
        """A tug finishing must never mask the vessel having left."""
        p = project(_call(), [_ev("CRAFT_RELEASED"), _ev("DEPARTED")])
        assert p.latest_event == "DEPARTED"

    def test_the_ledger_releases_the_call(self):
        p = project(_call(), [_ev("CRAFT_ASSIGNED"), _ev("CRAFT_RELEASED")])
        assert p.craft_state == "Idle"

    def test_assignment_rows_alone_do_not_set_the_state(self):
        """The whole point of Step 3/4: no module outside the engine derives craft_state."""
        p = project(_call(), (), None, [_craft(STATUS_ASSIGNED)])
        assert p.craft_state == "Idle"

    def test_departure_ends_any_engagement(self):
        p = project(_call(), [_ev("CRAFT_ASSIGNED"), _ev("DEPARTED")])
        assert p.craft_state == "Idle"


class TestCraftCountComesFromTheRows:
    """The one craft fact the ledger cannot answer: a reached-set has no cardinality."""

    def test_no_rows_is_zero(self):
        assert project(_call()).craft_committed == 0

    def test_every_working_rung_counts(self):
        for st in (STATUS_ASSIGNED, STATUS_DISPATCHED, STATUS_ON_SCENE, STATUS_ASSISTING):
            p = project(_call(), (), None, [_craft(st)])
            assert p.craft_committed == 1, st

    def test_a_released_craft_no_longer_counts(self):
        assert project(_call(), (), None, [_craft(STATUS_RELEASED)]).craft_committed == 0

    def test_several_craft_are_counted(self):
        p = project(_call(), (), None,
                    [_craft(STATUS_ASSIGNED, 7), _craft(STATUS_ASSISTING, 8)])
        assert p.craft_committed == 2

    def test_a_partly_released_movement_counts_only_the_live_ones(self):
        """One tug standing down while another works keeps the call committed — which is
        why CRAFT_RELEASED is written only when the LAST craft stands down."""
        p = project(_call(), [_ev("CRAFT_ASSIGNED")], None,
                    [_craft(STATUS_ASSISTING, 7), _craft(STATUS_RELEASED, 8)])
        assert p.craft_committed == 1 and p.craft_state == "Committed"


class TestSeparationOfConcerns:
    def test_craft_state_does_not_disturb_the_engine_verdict(self):
        """`portcraft_state` is DEMAND (does this movement need craft); `craft_state` is
        SUPPLY (are any actually held). Conflating them would make a commitment look like
        a requirement and hide an uncovered movement."""
        bare = project(_call())
        with_craft = project(_call(), [_ev("CRAFT_ASSIGNED")])
        assert bare.portcraft_state == with_craft.portcraft_state

    def test_craft_does_not_touch_the_pilot_state(self):
        p = project(_call(), [_ev("CRAFT_ASSIGNED")], None, [_craft()])
        assert p.pilot_state == "Pending" and p.pilot_source is None

    def test_every_pre_existing_projection_field_survives(self):
        d = project(_call(), [_ev("CRAFT_ASSIGNED")], None, [_craft()]).to_dict()
        for f in ("status", "arrival_state", "berth_state", "pilot_state",
                  "departure_state", "shipping_state", "portcraft_state",
                  "is_in_port", "is_at_berth", "latest_event"):
            assert f in d, f


class TestLedgerOwnership:
    """manual_craft may WRITE the craft milestones; it may never derive state from them.

    Scans CODE, not prose: an earlier draft of these tests matched their own docstrings
    and the DELETE clause, which is exactly the false positive they exist to avoid.
    """

    @staticmethod
    def _code() -> str:
        """Source with docstrings and comments stripped, so only statements are scanned."""
        import io
        import tokenize
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "services/marine/manual_craft.py").read_text(encoding="utf-8")
        out, prev = [], tokenize.INDENT
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            # A STRING in statement position is a docstring, not a value.
            if tok.type == tokenize.STRING and prev in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT):
                prev = tok.type
                continue
            out.append(tok.string)
            if tok.type not in (tokenize.NL,):
                prev = tok.type
        return " ".join(out)

    def test_it_writes_the_craft_milestones(self):
        assert "INSERT INTO core.vessel_call_event" in self._code()

    def test_it_never_reads_state_back_from_the_ledger(self):
        """A SELECT here would be the first step towards deriving state outside the
        engine. DELETE is permitted: clearing a stale CRAFT_RELEASED on re-commitment is
        a write, not a read."""
        import re
        code = self._code()
        assert not re.search(r"SELECT[^\"']*FROM\s+core\.vessel_call_event", code, re.I)

    def test_it_does_not_derive_state(self):
        code = self._code()
        assert "derive_state" not in code
        # No assignment to craft_state anywhere — the engine owns that name.
        assert "craft_state =" not in code
