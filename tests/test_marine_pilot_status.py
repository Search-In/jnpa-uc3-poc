"""Pilotage business propagation — workflow status from the Marine Projection Layer.

Pilotage had NO status field and NO static lifecycle logic to replace: the router exposed
raw timestamps only. What it lacked was a workflow position, which is now derived from the
shared projection plus the pilotage row's OWN movement_type and timestamps.

These tests pin: the five-state vocabulary, that no lifecycle is derived locally, that the
response shape is untouched, and that the projection is the only lifecycle source.

Pure: no DB, no corpus.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from services.marine import pilot_status as PS
from services.marine.projection import project

REPO = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.datetime(2026, 7, 29, 12, 0, tzinfo=IST)


def proj(*events, **call):
    call.setdefault("call_id", 1)
    return project(call, [{"event_type": e, "event_ts": T} for e in events])


def row(**kw):
    base = {"pilotage_id": 1, "movement_type": "INWARD", "via_no": "S0776",
            "pilot_boarded_at": None, "pilot_disembarked_at": None,
            "all_fast_at": None, "submitted_at": None, "extras": None}
    base.update(kw)
    return base


# ---------------------------------------------------------------- the five states
class TestWorkflowVocabulary:
    def test_all_five_states_exist_in_order(self):
        assert PS.WORKFLOW == ("Planned", "Pilot Requested", "Pilot Boarded",
                               "Pilot Completed", "Departure Pilot Completed")

    def test_planned_when_nothing_has_happened(self):
        assert PS.derive(row(), proj()) == PS.PLANNED

    def test_requested_when_the_memo_is_lodged(self):
        assert PS.derive(row(submitted_at=T), proj()) == PS.REQUESTED

    def test_boarded_from_the_cards_own_timestamp(self):
        assert PS.derive(row(pilot_boarded_at=T), proj()) == PS.BOARDED

    def test_boarded_from_the_projection_when_the_card_is_blank(self):
        """The PCS stream saw the boarding; the pilot card had not recorded it."""
        assert PS.derive(row(), proj("ANCHORED", "PILOT_BOARDED")) == PS.BOARDED

    def test_inward_completion_is_pilot_completed(self):
        assert PS.derive(row(movement_type="INWARD", pilot_boarded_at=T,
                             pilot_disembarked_at=T), proj()) == PS.COMPLETED

    def test_outward_completion_is_departure_pilot_completed(self):
        """Only movement_type can say WHICH pilot job finished — the projection describes
        the call, and a call has several movements."""
        assert PS.derive(row(movement_type="OUTWARD", pilot_boarded_at=T,
                             pilot_disembarked_at=T), proj()) == PS.DEPARTURE_COMPLETED

    def test_completion_can_come_from_the_lifecycle(self):
        """Card blank, but the call reached BERTHED so the inbound pilot job is done."""
        assert PS.derive(row(movement_type="INWARD"),
                         proj("PILOT_BOARDED", "BERTHED")) == PS.COMPLETED

    def test_shifting_completes_as_pilot_completed(self):
        assert PS.derive(row(movement_type="SHIFTING", pilot_disembarked_at=T),
                         proj()) == PS.COMPLETED

    def test_works_with_no_projection_at_all(self):
        """A pilot card can be imported before its PCS call exists."""
        assert PS.derive(row(), None) == PS.PLANNED
        assert PS.derive(row(submitted_at=T), None) == PS.REQUESTED
        assert PS.derive(row(pilot_boarded_at=T), None) == PS.BOARDED
        assert PS.derive(row(pilot_disembarked_at=T), None) == PS.COMPLETED

    def test_output_is_always_one_of_the_five(self):
        for mv in ("INWARD", "OUTWARD", "SHIFTING", "", None):
            for events in ([], ["PILOT_BOARDED"], ["BERTHED"], ["DEPARTED"]):
                assert PS.derive(row(movement_type=mv), proj(*events)) in PS.WORKFLOW


# ---------------------------------------------------------------- timestamps
class TestEffectiveTimestamps:
    def test_card_time_wins_over_the_projection(self):
        card = dt.datetime(2026, 7, 29, 8, 0, tzinfo=IST)
        out = PS.effective_times(row(pilot_boarded_at=card), proj("PILOT_BOARDED"))
        assert out["pilot_boarded_at"] == card

    def test_projection_fills_a_gap_the_card_left(self):
        out = PS.effective_times(row(), proj("PILOT_BOARDED"))
        assert out["pilot_boarded_at"] == T

    def test_berthed_milestone_supplies_all_fast_when_absent(self):
        out = PS.effective_times(row(), proj("BERTHED"))
        assert out["all_fast_at"] == T

    def test_absent_times_are_omitted_not_nulled(self):
        out = PS.effective_times(row(), proj())
        assert out == {}

    def test_departure_time_comes_from_the_lifecycle(self):
        assert PS.effective_times(row(), proj("DEPARTED"))["departed_at"] == T


# ---------------------------------------------------------------- shape
class TestShapePreserved:
    def test_apply_keeps_every_key_and_adds_none(self):
        r = row()
        assert set(PS.apply(r, proj("PILOT_BOARDED"))) == set(r)

    def test_status_lands_in_the_existing_extras_field(self):
        out = PS.apply(row(pilot_boarded_at=T), proj())
        assert out["extras"]["lifecycle"]["pilot_status"] == PS.BOARDED

    def test_namespaced_so_it_cannot_collide_with_a_sheet_column(self):
        out = PS.apply(row(extras={"pilot_status": "from the sheet"}), proj())
        assert out["extras"]["pilot_status"] == "from the sheet"
        assert out["extras"]["lifecycle"]["pilot_status"] == PS.PLANNED

    def test_existing_extras_keys_survive(self):
        out = PS.apply(row(extras={"remarks": "Escort tug OCEAN CREST used"}), proj())
        assert out["extras"]["remarks"] == "Escort tug OCEAN CREST used"

    def test_apply_does_not_mutate_the_input(self):
        r = row(extras={"a": 1})
        PS.apply(r, proj("BERTHED"))
        assert r["extras"] == {"a": 1}

    def test_response_model_is_unchanged(self):
        from gateway.routers.marine_pilotage import PilotageOut, PilotageStatsOut
        assert len(PilotageOut.model_fields) == 22
        assert len(PilotageStatsOut.model_fields) == 3


# ---------------------------------------------------------------- no duplication
class TestSingleSourceOfTruth:
    MOD = (REPO / "services/marine/pilot_status.py").read_text(encoding="utf-8")
    SVC = (REPO / "services/marine/pilotage.py").read_text(encoding="utf-8")

    def test_translation_module_derives_no_lifecycle(self):
        for banned in ("derive_state", "EVENT_ORDER", "array_position",
                       "core.vessel_call", "BERTHED\"", "DEPARTED\""):
            assert banned not in self.MOD, f"pilot_status derives lifecycle: {banned}"

    def test_service_consumes_the_projection(self):
        assert "MarineProjection" in self.SVC
        assert "self._projection.by_vias(" in self.SVC

    def test_service_never_calls_derive_state(self):
        assert "derive_state" not in self.SVC

    def test_service_never_queries_the_event_ledger(self):
        assert "vessel_call_event" not in self.SVC

    def test_no_via_resolution_is_re_implemented(self):
        """The recycled-VIA tiebreak belongs to the projection alone."""
        assert "LATERAL" not in self.SVC
        assert "ORDER BY eta DESC" not in self.SVC

    def test_lookup_is_batched_per_page(self):
        assert self.SVC.count("by_vias(") == 1

    def test_keyed_on_via_not_call_id(self):
        """core.pilotage.call_id resolves only for rows imported after the call existed,
        so keying on it would leave older cards permanently unenriched."""
        assert 'r.get("via_no")' in self.SVC

    def test_no_writes(self):
        for banned in ("INSERT", "UPDATE ", "DELETE"):
            assert banned not in self.SVC.upper().replace("UPDATED_AT", "")

    def test_no_schema_change(self):
        from gateway.marine_ext import _DDL
        assert not any("pilot_status" in s for s in _DDL)


# ============================================================ PORT CRAFT propagation
class TestPortCraftDemand:
    """Port Craft consumes the projection. It reports DEMAND against CAPACITY and never
    invents a per-craft assignment: core.port_craft has no operational state column and
    nothing in the schema links a craft to a call."""

    SVC = (REPO / "services/marine/state_service.py").read_text(encoding="utf-8")
    ROUTER = (REPO / "gateway/routers/marine_state.py").read_text(encoding="utf-8")

    def test_service_consumes_the_projection(self):
        block = self.SVC.split("async def port_craft_demand")[1]
        assert "self._projection.by_call_ids(" in block

    def test_no_derive_state_call(self):
        assert "derive_state" not in self.SVC

    def test_no_direct_event_ledger_query(self):
        """SQL only — a docstring naming the ledger is documentation, not a query."""
        import re
        assert not re.search(r"(?:FROM|JOIN|INTO|UPDATE)\s+core\.vessel_call_event",
                             self.SVC, re.I)

    def test_phase_is_read_off_engine_fields_not_re_derived(self):
        block = self.SVC.split("async def port_craft_demand")[1]
        for f in ("portcraft_state", "departure_state", "is_at_berth", "pilot_state"):
            assert f"p.{f}" in block, f"phase must come from the engine field {f}"
        # No milestone names re-tested locally.
        for milestone in ('"BERTHED"', '"PILOT_BOARDED"', '"DEPARTED"'):
            assert milestone not in block, "port craft re-derives milestones"

    def test_craft_engagement_verdict_is_the_engines(self):
        block = self.SVC.split("async def port_craft_demand")[1]
        assert 'p.portcraft_state != "Busy"' in block

    def test_no_utilisation_percentage_is_published(self):
        """Converting movements into craft-engaged needs an assumed craft-per-movement
        ratio that is not in the data, so no ratio is published.

        Asserted against the RESPONSE MODEL and the returned payload — a docstring
        explaining the omission is documentation, not a published value.
        """
        from gateway.routers.marine_state import PortCraftDemandOut
        fields = set(PortCraftDemandOut.model_fields)
        assert not any(("util" in f) or ("percent" in f) or ("ratio" in f)
                       for f in fields), fields
        # And nothing in the aggregation divides demand by fleet size.
        block = self.SVC.split("async def port_craft_demand")[1]
        assert "/ len(" not in block and "/ sum(" not in block

    def test_no_per_craft_assignment_is_claimed(self):
        block = self.SVC.split("async def port_craft_demand")[1]
        assert "craft_id" not in block, "no craft is assigned to a call"

    def test_fleet_capacity_comes_from_the_real_register(self):
        assert "FROM core.port_craft" in self.SVC

    def test_existing_port_craft_contract_is_untouched(self):
        from gateway.routers.marine_port_craft import PortCraftOut, PortCraftStatsOut
        assert len(PortCraftOut.model_fields) == 14
        assert len(PortCraftStatsOut.model_fields) == 3

    def test_route_is_registered_and_read_only(self):
        from gateway.routers import marine_state
        paths = {(tuple(sorted(r.methods)), r.path) for r in marine_state.router.routes}
        assert (("GET",), "/api/marine/state/port-craft") in paths
        assert all(m == ("GET",) for m, _ in paths)

    def test_no_schema_change(self):
        from gateway.marine_ext import _DDL
        joined = " ".join(_DDL)
        assert "craft_status" not in joined and "utilisation" not in joined.lower()
