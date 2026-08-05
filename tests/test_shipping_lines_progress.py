"""Shipping-Lines business propagation — vessel progress from the Marine Projection Layer.

Shipping Lines had NO lifecycle field to replace: its only "status" columns are cargo
attributes (load_status F/E, reefer_status, equipment_status, con_seal_status), and the
module was fully isolated from core.vessel_call. What it lacked was vessel progress, now
derived from the shared projection and keyed by vessel_visit.

Pure: no DB, no corpus.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from services.marine.projection import project
from services.shipping_lines import vessel_progress as VP

REPO = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
T = dt.datetime(2026, 7, 29, 12, 0, tzinfo=IST)


def proj(*events, **call):
    call.setdefault("call_id", 1)
    call.setdefault("via_no", "S0276")
    return project(call, [{"event_type": e, "event_ts": T} for e in events])


# ---------------------------------------------------------------- key resolution
class TestViaCandidates:
    def test_plain_via_is_used_as_is(self):
        assert VP.via_candidates("S0276") == ["S0276"]

    def test_composite_tries_exact_first_then_the_stripped_form(self):
        """Doc 01 §1.9 documents the composite ('strip 3-char vessel code'); exact still
        wins so a stored composite via_no is never bypassed."""
        assert VP.via_candidates("KMIS0276") == ["KMIS0276", "S0276"]

    def test_documented_dp_world_composites_resolve(self):
        assert VP.via_candidates("CGKS0504")[-1] == "S0504"
        assert VP.via_candidates("AGLS0540")[-1] == "S0540"

    def test_case_and_space_tolerant(self):
        assert VP.via_candidates("  kmis0276 ") == ["KMIS0276", "S0276"]

    def test_a_non_via_is_never_turned_into_one(self):
        for junk in ("", None, "RANDOM", "1234", "ABCDEFGH", "S02"):
            assert VP.via_candidates(junk) == []

    def test_candidates_are_batched_and_deduplicated(self):
        rows = [{"vessel_visit": "KMIS0276"}, {"vessel_visit": "S0276"},
                {"vessel_visit": None}]
        assert VP.all_candidates(rows) == ["KMIS0276", "S0276"]


class TestResolve:
    def test_exact_match_is_reported_as_exact(self):
        p = proj()
        got, how = VP.resolve("S0276", {"S0276": p})
        assert got is p and how == VP.EXACT

    def test_composite_match_is_labelled_not_disguised_as_exact(self):
        p = proj()
        got, how = VP.resolve("KMIS0276", {"S0276": p})
        assert got is p and how == VP.COMPOSITE

    def test_exact_wins_when_both_forms_exist(self):
        exact, stripped = proj(call_id=1), proj(call_id=2)
        got, how = VP.resolve("KMIS0276", {"KMIS0276": exact, "S0276": stripped})
        assert got is exact and how == VP.EXACT

    def test_unresolved_is_reported_not_fabricated(self):
        assert VP.resolve("S9999", {}) == (None, None)
        assert VP.resolve("RANDOM", {"S0276": proj()}) == (None, None)


# ---------------------------------------------------------------- no duplication
class TestSingleSourceOfTruth:
    MOD = (REPO / "services/shipping_lines/vessel_progress.py").read_text(encoding="utf-8")
    SVC = (REPO / "services/marine/state_service.py").read_text(encoding="utf-8")
    SL_REPO = (REPO / "services/shipping_lines/repository.py").read_text(encoding="utf-8")

    def test_key_module_derives_no_lifecycle(self):
        for banned in ("derive_state", "EVENT_ORDER", "array_position",
                       "vessel_call_event", "BERTHED", "DEPARTED"):
            assert banned not in self.MOD, f"vessel_progress derives lifecycle: {banned}"

    def test_progress_is_read_off_projection_fields(self):
        block = self.SVC.split("async def shipping_line_progress")[1]
        for f in ("p.status", "p.arrival_state", "p.berth_state", "p.departure_state",
                  "p.is_in_port", "p.is_at_berth"):
            assert f in block, f"progress must come from the projection field {f}"

    def test_active_vs_historical_is_the_engines_verdict(self):
        block = self.SVC.split("async def shipping_line_progress")[1]
        assert "active = p.is_in_port" in block
        # Not a local date comparison.
        assert "datetime.now" not in block and "utcnow" not in block

    def test_service_uses_the_projection_only(self):
        """SQL only — a docstring naming the ledger is documentation, not a query.
        (Same rule the projection layer's own repo-wide scan applies.)"""
        import re
        assert "self._projection.by_vias(" in self.SVC
        assert "derive_state" not in self.SVC
        assert not re.search(r"(?:FROM|JOIN|INTO|UPDATE)\s+core\.vessel_call_event",
                             self.SVC, re.I)

    def test_shipping_lines_repository_is_untouched_by_lifecycle(self):
        assert "vessel_call" not in self.SL_REPO
        assert "derive_state" not in self.SL_REPO

    def test_no_via_resolution_lateral_is_re_implemented(self):
        assert "LATERAL" not in self.MOD

    def test_lookup_is_batched(self):
        block = self.SVC.split("async def shipping_line_progress")[1]
        assert block.count("by_vias(") == 1


class TestNotFabricated:
    def test_current_position_is_not_published(self):
        """No coordinate exists on core.vessel_call or core.vessel_call_event, so a
        position would have to be invented. Live position is the AIS layer's."""
        from gateway.routers.marine_state import SlLifecycleOut
        f = set(SlLifecycleOut.model_fields)
        assert not any(k in f for k in ("position", "lat", "lon", "latitude", "longitude"))

    def test_match_quality_is_always_reported(self):
        from gateway.routers.marine_state import SlVisitOut
        assert "match" in SlVisitOut.model_fields

    def test_unmatched_visits_are_counted_not_dropped(self):
        from gateway.routers.marine_state import ShippingLineProgressOut
        f = set(ShippingLineProgressOut.model_fields)
        assert {"matched", "unmatched", "matched_exact", "matched_composite"} <= f


class TestContractsUnchanged:
    def test_shipping_lines_api_is_untouched(self):
        from gateway.routers.shipping_lines import ImportResponse, Page
        assert len(Page.model_fields) == 5
        assert len(ImportResponse.model_fields) >= 1

    def test_other_module_contracts_are_untouched(self):
        from gateway.routers.berthing import ReportOut
        from gateway.routers.marine_calls import CallOut
        from gateway.routers.marine_pilotage import PilotageOut
        from gateway.routers.marine_port_craft import PortCraftOut
        assert (len(CallOut.model_fields), len(PilotageOut.model_fields),
                len(PortCraftOut.model_fields), len(ReportOut.model_fields)) == (24, 22, 14, 17)

    def test_route_is_registered_and_read_only(self):
        from gateway.routers import marine_state
        paths = {(tuple(sorted(r.methods)), r.path) for r in marine_state.router.routes}
        assert (("GET",), "/api/marine/state/shipping-lines") in paths
        assert all(m == ("GET",) for m, _ in paths)

    def test_no_schema_change(self):
        from gateway.marine_ext import _DDL
        assert not any("vessel_progress" in s for s in _DDL)
