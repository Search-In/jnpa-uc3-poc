"""CALINV (Allotment of VCN) tests — corpus mapping + lifecycle placement.

CALINV is the PCS response that ISSUES the Vessel Call Number (DocumentName
``ALLOTMENTOFVCN``). Like BERALT it has no standalone XML in the corpus: all 546 messages
arrive as rows of an NLP Outbound journal, so these run through the real delivery path.

Tier 1 — field mapping against the client files.
Tier 2 — lifecycle ordering (pure; no corpus, no database).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from services.marine.parsers import REGISTRY, parse_marine
from services.marine.parsers.pcs_common import (CALL_STATUS_BERTH_ALLOTTED,
                                                CALL_STATUS_BERTH_PLANNED,
                                                CALL_STATUS_PLANNED,
                                                CALL_STATUS_VCN_ALLOTTED)

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))
OUTBOUND = DATA_DIR / "Outbound_CALINV_BERALT"

corpus = pytest.mark.skipif(not OUTBOUND.is_dir(),
                            reason=f"outbound journal absent: {OUTBOUND}")


def _calinv() -> list[dict]:
    out: list[dict] = []
    for f in sorted(OUTBOUND.glob("*.csv")):
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected, f"{f.name} rejected"
        out.extend(r for r in res.records if r["_message"] == "CALINV")
    return out


# ---------------------------------------------------------------- Tier 1 — mapping
@corpus
class TestCalinvMapping:
    def test_all_546_allotments_parse(self):
        assert len(_calinv()) == 546

    def test_it_is_registered_so_the_journal_no_longer_rejects_it(self):
        """Before this slice, 546 CALINV rows were counted as invalid, which made SUCCESS
        unreachable for any mixed journal."""
        assert "CALINV" in REGISTRY

    def test_the_small_journal_now_imports_cleanly(self):
        """104 documents = 45 BERALT + 59 CALINV, with nothing left unroutable."""
        f = OUTBOUND / "NLP Outbound Data_CALINV_BERALT.csv"
        res = parse_marine(f.read_bytes(), f.name)
        assert res.invalid_count == 0
        assert not res.rejected

    def test_every_message_carries_the_vcn_it_allots(self):
        recs = _calinv()
        assert all(c["vcn"] for c in recs)
        assert all(len(c["vcn"]) == 14 for c in recs)

    def test_via_is_recovered_from_the_vcn_tail(self):
        recs = _calinv()
        assert all(c["via_no"] == c["vcn"][9:] for c in recs)

    def test_voyage_number_is_populated_unlike_beralt(self):
        """CALINV supplies the voyage number BERALT cannot — its tag is empty 364/364."""
        recs = _calinv()
        assert all(c["voyage_no"] for c in recs)

    def test_eta_and_etd_are_mapped(self):
        recs = _calinv()
        assert all(isinstance(c["eta"], dt.datetime) for c in recs)
        assert all(isinstance(c["etd"], dt.datetime) for c in recs)

    def test_terminal_comes_from_the_vcn_infix_not_dockortocode(self):
        """DockORTOCode is EMPTY on 535 of 546, and on the one populated message where the
        two disagree it is the known NSFT/JNPCT ambiguity. The infix resolves 545/546."""
        recs = _calinv()
        resolved = [c for c in recs if c["terminal_code"]]
        assert len(resolved) >= 545
        assert {c["terminal_code"] for c in resolved} <= {
            "NSICT", "NSFT", "APMT", "NSIGT", "BMCT", "NSDT"}

    def test_status_is_the_vcn_allotment_stage(self):
        assert all(c["status"] == CALL_STATUS_VCN_ALLOTTED for c in _calinv())

    def test_no_berth_is_claimed(self):
        """The berth arrives with BERALT; CALINV must not pre-empt it."""
        assert all(c["berth_code"] is None for c in _calinv())

    def test_no_etb_is_claimed(self):
        """Expected berthing arrives with BERMAN's EDB."""
        assert all(c["etb"] is None for c in _calinv())

    def test_no_event_is_emitted_from_a_date_only_field(self):
        """AllotmentDate is date-only on all 546 (8 chars). event_ts is timestamptz NOT
        NULL, so emitting one would stamp every allotment at midnight."""
        for f in sorted(OUTBOUND.glob("*.csv")):
            res = parse_marine(f.read_bytes(), f.name)
            evs = [r for r in res.records
                   if r["_target"] == "vessel_call_event" and r["_message"] == "CALINV"]
            assert evs == []

    def test_allotment_date_is_still_parsed_for_later_use(self):
        recs = _calinv()
        dated = [c["allotment_date"] for c in recs if c["allotment_date"] is not None]
        assert len(dated) == len(recs)
        assert all(isinstance(d, dt.date) for d in dated)


# ---------------------------------------------------------------- Tier 2 — lifecycle
class TestLifecyclePlacement:
    """CALINV sits between CALINF and BERMAN once the journals are read by direction:
    CALINF (in) -> CALINV (out) -> BERMAN (in) -> BERALT (out)."""

    def test_status_rank_order_matches_the_message_flow(self):
        from services.marine.repository import _STATUS_ORDER
        i = {s: _STATUS_ORDER.index(f"'{s}'") for s in (
            CALL_STATUS_PLANNED, CALL_STATUS_VCN_ALLOTTED,
            CALL_STATUS_BERTH_PLANNED, CALL_STATUS_BERTH_ALLOTTED)}
        assert (i[CALL_STATUS_PLANNED] < i[CALL_STATUS_VCN_ALLOTTED]
                < i[CALL_STATUS_BERTH_PLANNED] < i[CALL_STATUS_BERTH_ALLOTTED])

    def test_vcn_allotted_cannot_overwrite_a_later_stage(self):
        """A CALINV replayed after BERALT must not rewind the call — the guard ranks it
        below both berth stages."""
        from services.marine.repository import _STATUS_ORDER, _VESSEL_CALL_UPSERT
        assert f"'{CALL_STATUS_VCN_ALLOTTED}'" in _STATUS_ORDER
        assert "array_position" in _VESSEL_CALL_UPSERT

    def test_calinv_reuses_the_shared_call_upsert(self):
        """No new persistence path: CALINV is VCN-keyed, so it lands in the same
        calls_vcn partition BERMAN and BERALT use."""
        import inspect

        from services.marine import repository as R
        src = inspect.getsource(R.VesselCallRepository.persist)
        assert "calls_vcn" in src
        assert src.count("_VESSEL_CALL_UPSERT") >= 1
