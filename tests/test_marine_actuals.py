"""VESARR / VESDEP actuals — event coverage, call resolution, and the ata/atd projection.

The parsers already emitted the six milestones; what was missing was reading them back onto
core.vessel_call, so every KPI aggregate in stats() (arrived / in_port / ops_completed /
avg_turnaround_hours / avg_pre_berth_delay_hours) was permanently 0 or NULL.

Scope pinned by these tests, per the agreed decisions:
  * ata <- ARRIVED, atd <- DEPARTED (the official ATD event)
  * NO atb column, NO KPI formula change, NO schema change
  * atc stays NULL — no NLP Marine message carries cargo-complete

Tier 1 runs against the client .log files; Tier 2 is pure SQL/text assertion.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.marine import repository as R
from services.marine.parsers import parse_marine
from services.marine.parsers.vesarr_vesdep import _EVENT_MAP

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))

corpus = pytest.mark.skipif(not (DATA_DIR / "VESARR").is_dir(),
                            reason=f"VESARR client data absent: {DATA_DIR}")

#: The six milestones agreed for persistence.
EXPECTED = {"ANCHORED", "PILOT_BOARDED", "BERTHED", "ARRIVED", "SAILED", "DEPARTED"}


def _events(sub: str) -> list[dict]:
    out: list[dict] = []
    for f in sorted((DATA_DIR / sub).glob("*")):
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected, f"{f.name} rejected"
        out.extend(r for r in res.records if r["_target"] == "vessel_call_event")
    return out


# ---------------------------------------------------------------- Tier 1 — corpus
@corpus
class TestActualsParse:
    def test_all_six_milestones_are_mapped(self):
        mapped = {t for pairs in _EVENT_MAP.values() for t, _ in pairs}
        assert mapped == EXPECTED

    def test_vesarr_emits_its_arrival_milestones(self):
        types = {e["event_type"] for e in _events("VESARR")}
        assert types == {"ANCHORED", "PILOT_BOARDED", "BERTHED", "ARRIVED"}

    def test_vesdep_emits_its_departure_milestones(self):
        types = {e["event_type"] for e in _events("VESDEP")}
        assert types == {"PILOT_BOARDED", "DEPARTED", "SAILED"}

    def test_every_event_is_timed(self):
        """An absent milestone is skipped, never stamped with a fabricated time."""
        for sub in ("VESARR", "VESDEP"):
            assert all(e["event_ts"] is not None for e in _events(sub))

    def test_every_event_carries_the_three_resolution_keys(self):
        """VCN -> (imo, voyage) -> VIA. imo_no was missing, which left tier 2 dead."""
        for sub in ("VESARR", "VESDEP"):
            evs = _events(sub)
            assert all(e.get("vcn") for e in evs)
            assert all(e.get("imo_no") for e in evs), "tier-2 resolution key absent"
            assert all(e.get("via_no") for e in evs)

    def test_berth_value_may_not_be_a_berth(self):
        """VESARR/VESDEP BerthNumber carries 'SEA' and 'POINT-2' as well as real codes, so
        the event berth MUST stay resolve-or-NULL and never create a berth."""
        vals = {e.get("berth_code") for e in _events("VESARR") + _events("VESDEP")}
        assert "SEA" in vals or "POINT-2" in vals

    def test_documented_tss_amber_chain_reproduces(self):
        """Doc 01 §1.5: anchored 29-07 05:18 -> pilot 19:30 -> berthed 21:24."""
        amber = [e for e in _events("VESARR") if e["vcn"] == "INNSA1NF0S0776"]
        by = {e["event_type"]: e["event_ts"] for e in amber}
        assert by["ANCHORED"].strftime("%d-%m %H:%M") == "29-07 05:18"
        assert by["PILOT_BOARDED"].strftime("%H:%M") == "19:30"
        assert by["BERTHED"].strftime("%H:%M") == "21:24"
        assert all(e.get("via_no") == "S0776" for e in amber)
        assert all(e.get("voyage_no") == "2626" for e in amber)


# ---------------------------------------------------------------- Tier 2 — projection
class TestActualsProjection:
    def test_projection_reads_the_ledger_not_the_batch(self):
        """Idempotent and self-healing: a call whose milestones arrived across several
        files still resolves, and a re-import recomputes the same value."""
        sql = R._PROJECT_CALL_ACTUALS
        assert "FROM core.vessel_call_event" in sql
        assert "min(event_ts) FILTER" in sql

    def test_ata_comes_from_arrived(self):
        assert "FILTER (WHERE event_type = 'ARRIVED')" in R._PROJECT_CALL_ACTUALS
        assert "ata = COALESCE(a.arrived,  c.ata)" in R._PROJECT_CALL_ACTUALS

    def test_atd_comes_from_departed_not_sailed(self):
        """DEPARTED is the official ATD event. SAILED precedes it in the corpus
        (02:15 vs 04:40), so picking the wrong one would understate turnaround."""
        assert "FILTER (WHERE event_type = 'DEPARTED')" in R._PROJECT_CALL_ACTUALS
        assert "atd = COALESCE(a.departed, c.atd)" in R._PROJECT_CALL_ACTUALS
        assert "'SAILED'" not in R._PROJECT_CALL_ACTUALS

    def test_atc_is_never_written(self):
        """No NLP Marine message carries cargo-complete; Pre-Sailing Delay stays honestly
        uncomputable rather than guessed."""
        assert "atc" not in R._PROJECT_CALL_ACTUALS

    def test_no_atb_column_is_introduced(self):
        """BERTHED stays an event only — the agreed no-schema-change constraint."""
        assert "atb" not in R._PROJECT_CALL_ACTUALS
        from gateway.marine_ext import _DDL
        assert not any("atb " in s or "atb," in s for s in _DDL)

    def test_projection_runs_for_every_touched_call(self):
        import inspect
        src = inspect.getsource(R.VesselCallRepository.persist)
        assert "touched_calls.add(cid)" in src
        assert "_PROJECT_CALL_ACTUALS" in src

    def test_projection_also_runs_on_a_duplicate_milestone(self):
        """A re-import whose events all dedupe must still back-fill a call whose ata/atd
        were never populated by an earlier build."""
        import inspect
        src = inspect.getsource(R.VesselCallRepository.persist)
        after_dup = src.split("same actual already stored")[1]
        assert "touched_calls.add(cid)" in after_dup

    def test_projection_is_not_counted_as_an_insert_or_update(self):
        """It is a derived roll-up of rows already counted; counting it again would
        misreport the import the way the 45-vs-44 mismatch did."""
        import inspect
        src = inspect.getsource(R.VesselCallRepository.persist)
        # Anchor on the step header itself — 'step 4b' is also referenced in step 4's
        # comment, so a bare '4b.' split captures the counting loop above it.
        block = src.split("# 4b. Roll the ledger")[1].split("# 5.")[0]
        assert "ins +=" not in block and "upd +=" not in block
        assert "_PROJECT_CALL_ACTUALS" in block

    def test_kpi_aggregates_still_read_the_same_columns(self):
        """The projection must feed stats() as-is — no formula was changed.

        The in-port predicate is now composed by state_engine.in_port_sql() rather than
        written inline, so assert the RENDERED predicate rather than the source text —
        otherwise this test pins the duplication the engine exists to remove.
        """
        import inspect

        from services.marine.state_engine import in_port_sql

        src = inspect.getsource(R.VesselCallRepository.stats)
        assert "c.ata IS NOT NULL" in src          # `arrived` counter, still inline
        assert "avg_pre_berth_delay_hours" in src
        # The composed predicate is byte-identical to what used to be written out.
        assert in_port_sql("c") == "c.ata IS NOT NULL AND c.atd IS NULL"
        assert "in_port_sql('c')" in src or "in_port_sql(\"c\")" in src
