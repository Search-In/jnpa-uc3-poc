"""Berthing business propagation — lifecycle-derived status, response shape preserved.

The berthing module now reads core.vessel_call + core.vessel_call_event and advances each
report's status from the marine lifecycle. These tests pin the three properties that make
that safe:

  1. translation only — no state is derived here, and no new status value is invented;
  2. advance-only — the stored PDF status can never be regressed;
  3. shape-preserving — rows keep every key and gain none, so ReportOut is untouched.

Pure: no DB, no corpus.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from services.berthing import lifecycle as LC
from services.marine.state_engine import derive_state

REPO = Path(__file__).resolve().parents[1]
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def st(*events: str, status: str | None = None):
    return derive_state(
        {"status": status},
        [{"event_type": e, "event_ts": dt.datetime(2026, 7, 29, 12, 0, tzinfo=IST)}
         for e in events])


# ---------------------------------------------------------------- business flow
class TestBusinessFlow:
    def test_calinf_only_is_expected(self):
        """CALINF -> ETA available."""
        assert LC.from_call_state(st(status="Planned")) == "EXPECTED"

    def test_berman_is_berth_assigned(self):
        """BERMAN -> berth assigned."""
        assert LC.from_call_state(st(status="Berth Planned")) == "BERTH_ASSIGNED"

    def test_beralt_is_berth_assigned(self):
        """BERALT -> berth allotted. Berthing has no separate 'allotted' value; the berth
        is assigned either way, and inventing one would break the CHECK constraint."""
        assert LC.from_call_state(
            st("BERTH_ALLOTTED", status="Berth Allotted")) == "BERTH_ASSIGNED"

    def test_vesarr_anchored_is_arrived(self):
        assert LC.from_call_state(st("BERTH_ALLOTTED", "ANCHORED")) == "ARRIVED"

    def test_vesarr_berthed_is_berthing_started(self):
        """VESARR -> vessel arrived / berthed."""
        assert LC.from_call_state(
            st("BERTH_ALLOTTED", "ANCHORED", "PILOT_BOARDED", "BERTHED")) == "BERTHING_STARTED"

    def test_vesdep_is_departed(self):
        """VESDEP -> vessel departed / berth released."""
        assert LC.from_call_state(st("BERTHED", "ARRIVED", "SAILED", "DEPARTED")) == "DEPARTED"

    def test_no_call_state_yields_nothing(self):
        assert LC.from_call_state(st()) is None


class TestNoInventedValues:
    def test_output_is_always_an_existing_berthing_status(self):
        """Anything else would violate core.berthing_record's CHECK constraint."""
        for events in ([], ["BERTH_ALLOTTED"], ["ANCHORED"], ["BERTHED"],
                       ["BERTHED", "ARRIVED"], ["SAILED"], ["DEPARTED"]):
            out = LC.from_call_state(st(*events, status="Berth Planned"))
            assert out is None or out in LC.LIFECYCLE

    def test_cargo_states_are_never_claimed_from_the_lifecycle(self):
        """No NLP Marine message reports cargo work, so claiming these would be invention.
        They reach the API only from the PDF, via the advance-only merge."""
        produced = {LC.from_call_state(st(*e, status="Berth Allotted"))
                    for e in ([], ["BERTH_ALLOTTED"], ["ANCHORED"], ["BERTHED"],
                              ["DEPARTED"])}
        assert "CARGO_OPERATION" not in produced
        assert "COMPLETED" not in produced

    def test_ladder_matches_the_check_constraint(self):
        ddl = (REPO / "gateway/berthing_ext.py").read_text(encoding="utf-8")
        for s in LC.LIFECYCLE:
            assert f"'{s}'" in ddl, f"{s} is not in the berthing CHECK constraint"


# ---------------------------------------------------------------- merge safety
class TestAdvanceOnly:
    def test_lifecycle_advances_a_stale_report(self):
        """The PDF still says berthing started; the PCS stream says the vessel sailed."""
        assert LC.effective_status("BERTHING_STARTED", st("BERTHED", "DEPARTED")) == "DEPARTED"

    def test_cargo_operation_is_not_downgraded(self):
        """THE regression this rule exists to prevent: only the PDF sees cargo work, so a
        lifecycle that only knows BERTHED must not pull the row back."""
        assert LC.effective_status("CARGO_OPERATION", st("BERTHED")) == "CARGO_OPERATION"

    def test_completed_is_not_downgraded(self):
        assert LC.effective_status("COMPLETED", st("BERTHED", "ARRIVED")) == "COMPLETED"

    def test_departed_is_never_regressed(self):
        assert LC.effective_status("DEPARTED", st("ANCHORED")) == "DEPARTED"

    def test_unmatched_report_keeps_its_stored_status_exactly(self):
        """A PDF-only row must behave as it did before this module existed."""
        for stored in LC.LIFECYCLE:
            assert LC.effective_status(stored, None) == stored

    def test_equal_rank_keeps_the_stored_value(self):
        assert LC.effective_status("ARRIVED", st("ANCHORED")) == "ARRIVED"

    def test_unknown_stored_status_never_wins(self):
        assert LC.rank("something else") == -1
        assert LC.effective_status("something else", st("BERTHED")) == "BERTHING_STARTED"


class TestShapePreserved:
    ROW = {"id": 1, "terminal": "BMCT", "vessel_name": "MSC REEF",
           "voyage_number": "S0672", "status": "ARRIVED", "eta": None}

    def test_apply_keeps_every_key_and_adds_none(self):
        out = LC.apply(self.ROW, st("BERTHED"))
        assert set(out) == set(self.ROW)

    def test_apply_changes_only_status(self):
        out = LC.apply(self.ROW, st("BERTHED"))
        assert out["status"] == "BERTHING_STARTED"
        for k in self.ROW:
            if k != "status":
                assert out[k] == self.ROW[k]

    def test_apply_does_not_mutate_the_input(self):
        LC.apply(self.ROW, st("DEPARTED"))
        assert self.ROW["status"] == "ARRIVED"

    def test_response_model_is_unchanged(self):
        from gateway.routers.berthing import ReportOut, StatsOut
        assert len(ReportOut.model_fields) == 17
        assert len(StatsOut.model_fields) == 9


# ---------------------------------------------------------------- single source of truth
class TestNoDuplicatedLogic:
    SRC = (REPO / "services/berthing/lifecycle.py").read_text(encoding="utf-8")
    REPO_SRC = (REPO / "services/berthing/repository.py").read_text(encoding="utf-8")

    def test_translation_module_derives_no_state(self):
        for banned in ("EVENT_ORDER", "array_position", "event_ts", "ANCHORED ="):
            assert banned not in self.SRC, f"lifecycle.py derives state itself: {banned}"

    SVC_SRC = (REPO / "services/berthing/service.py").read_text(encoding="utf-8")

    def test_service_delegates_to_the_shared_projection(self):
        """Lifecycle is fetched from MarineProjection, not derived here."""
        assert "MarineProjection" in self.SVC_SRC
        assert "self._projection.by_vias(" in self.SVC_SRC
        assert "derive_state" not in self.SVC_SRC

    def test_repository_owns_no_lifecycle_query(self):
        """The duplication the projection layer removed: berthing had its own events
        lookup and its own VIA-resolution LATERAL, both near-copies of state_service's."""
        assert "vessel_call" not in self.REPO_SRC
        assert "LATERAL" not in self.REPO_SRC
        assert "derive_state" not in self.REPO_SRC
        assert "lifecycle_by_voyage" not in self.REPO_SRC

    def test_stored_status_column_is_never_written_by_this_path(self):
        svc = (REPO / "services/berthing/service.py").read_text(encoding="utf-8")
        assert "UPDATE" not in svc.upper()

    def test_no_schema_change_was_made(self):
        ddl = (REPO / "gateway/berthing_ext.py").read_text(encoding="utf-8")
        assert "vessel_call" not in ddl, "berthing DDL must not reference the call spine"


class TestBatching:
    def test_lifecycle_is_fetched_once_per_page_not_per_row(self):
        svc = (REPO / "services/berthing/service.py").read_text(encoding="utf-8")
        assert svc.count("by_vias(") == 1, "must be batched over the page, not per-row"

    def test_empty_page_short_circuits(self):
        import inspect

        from services.berthing.service import BerthingService
        src = inspect.getsource(BerthingService._advance)
        assert "if not rows:" in src
