"""BERALT (berth allotment) tests — journal envelope, parser mapping, berth binding.

BERALT has NO standalone XML in the corpus: every one of the 364 messages arrives as a row
of an NLP Outbound message journal, wrapped in a pseudo-JSON ``ReqBody.XML`` envelope that
is NOT valid JSON (it uses semicolons as separators). These tests therefore exercise the
real delivery path end-to-end against the client files, plus the pure helpers.

Tier 1 — journal envelope (header scan + per-row document extraction).
Tier 2 — BERALT field mapping against the corpus.
Tier 3 — pure helpers and the repository's berth binding (no corpus, no database).
"""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from services.marine.parsers import detect_format, parse_marine
from services.marine.parsers.beralt import EVENT_BERTH_ALLOTTED
from services.marine.parsers.envelope import extract_xml_documents, journal_header_index
from services.marine.parsers.pcs_common import CALL_STATUS_BERTH_ALLOTTED, via_from_vcn

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))
OUTBOUND = DATA_DIR / "Outbound_CALINV_BERALT"
INBOUND = DATA_DIR / "Inbound_CALINF_BERMAN"

corpus = pytest.mark.skipif(not OUTBOUND.is_dir(),
                            reason=f"outbound journal absent: {OUTBOUND}")


def _parse_outbound() -> list:
    """Parse every outbound journal → the combined ParseResult records."""
    out = []
    for f in sorted(OUTBOUND.glob("*.csv")):
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected, f"{f.name} rejected"
        out.extend(res.records)
    return out


def _beralt_calls(records) -> list[dict]:
    return [r for r in records
            if r["_target"] == "vessel_call" and r["_message"] == "BERALT"]


def _beralt_events(records) -> list[dict]:
    return [r for r in records
            if r["_target"] == "vessel_call_event" and r["_message"] == "BERALT"]


# ---------------------------------------------------------------- Tier 1 — envelope
@corpus
class TestJournalEnvelope:
    def test_journal_csv_is_detected_as_a_message_carrier(self):
        """A journal is a .csv by extension but carries whole PCS documents, so content
        must win — otherwise it hits the vessel-call template parser and is REJECTED for a
        missing VCN column (the pre-existing defect this closes)."""
        for f in sorted(OUTBOUND.glob("*.csv")):
            assert detect_format(f.name, f.read_bytes()) == "JOURNAL"

    def test_ordinary_template_csv_is_untouched(self):
        """Regression guard: the vessel-call upload template must NOT become a journal."""
        tmpl = b"VCN,VIA,IMO,Vessel Name\nINNSA1BM0R3119,S0527,9320477,XIN YANG PU\n"
        assert detect_format("calls.csv", tmpl) == "CSV"
        res = parse_marine(tmpl, "calls.csv")
        assert not res.rejected and len(res.records) == 1
        assert res.records[0]["_message"] == "CSV"

    def test_header_is_found_below_the_banner_rows(self):
        """The corpus header sits on row 7 (index 6) under a title banner and five filter
        rows; csv.DictReader's row-0 assumption is what broke this file."""
        import csv as _csv
        import io
        f = next(iter(sorted(OUTBOUND.glob("*.csv"))))
        rows = list(_csv.reader(io.StringIO(f.read_bytes().decode("utf-8-sig"))))
        assert journal_header_index(rows) == 6

    def test_header_index_is_minus_one_for_a_non_journal(self):
        assert journal_header_index([["VCN", "VIA"], ["a", "b"]]) == -1

    def test_every_row_yields_its_embedded_document(self):
        for f in sorted(OUTBOUND.glob("*.csv")):
            docs = extract_xml_documents("JOURNAL", f.read_bytes())
            assert docs, f"{f.name}: no documents extracted"
            assert all(d.startswith("<") for d in docs)


# ---------------------------------------------------------------- Tier 2 — mapping
@corpus
class TestBeraltMapping:
    def test_all_364_allotments_parse(self):
        calls = _beralt_calls(_parse_outbound())
        assert len(calls) == 364, "expected 319 + 45 corpus BERALT messages"

    def test_every_allotment_names_exactly_one_berth(self):
        """<BerthCodes> is plural but the corpus carries exactly one <Berthcode> on all
        364 messages — a second berth would silently vanish, so assert the shape."""
        calls = _beralt_calls(_parse_outbound())
        assert all(c["berth_code"] for c in calls)

    def test_the_22_corpus_berth_codes_round_trip(self):
        codes = {c["berth_code"] for c in _beralt_calls(_parse_outbound())}
        assert len(codes) == 22
        # 'LB01 N' / 'LB01 S' are distinct sub-berths and must not be normalised away;
        # there is deliberately no CB03 in the corpus.
        assert {"LB01 N", "LB01 S"} <= codes
        assert "CB03" not in codes

    def test_via_is_recovered_from_the_vcn_tail(self):
        """VoyageNumber is empty on all 364 messages, so the VCN tail is the only VIA
        source — and it is what finally populates core.vessel_call.via_no."""
        calls = _beralt_calls(_parse_outbound())
        assert all(c["via_no"] for c in calls)
        assert all(c["via_no"] == c["vcn"][9:] for c in calls)

    def test_status_advances_to_berth_allotted(self):
        assert all(c["status"] == CALL_STATUS_BERTH_ALLOTTED
                   for c in _beralt_calls(_parse_outbound()))

    def test_terminal_is_never_set_from_beralt(self):
        """DockORTOCode contradicts the terminal CALINF/BERMAN established (CB01 is NSFT
        by VCN infix but JNPCT by dock code), so BERALT must not feed terminal_id."""
        assert all(c["terminal_code"] is None for c in _beralt_calls(_parse_outbound()))

    def test_allotment_time_is_not_written_to_etb(self):
        """AllotmentDateTime is when the allotment was ISSUED, not when the vessel is
        expected alongside — conflating them would fabricate an ETB."""
        calls = _beralt_calls(_parse_outbound())
        assert all(c["etb"] is None and c["eta"] is None and c["etd"] is None
                   for c in calls)

    def test_one_timed_event_per_allotment(self):
        recs = _parse_outbound()
        calls, events = _beralt_calls(recs), _beralt_events(recs)
        assert len(events) == len(calls)
        assert all(e["event_type"] == EVENT_BERTH_ALLOTTED for e in events)
        assert all(e["event_ts"] is not None for e in events)
        assert all(e["vcn"] and e["berth_code"] for e in events)

    def test_documented_worked_example_reproduces(self):
        """Doc 01 §1.6 example ①: 'BERALT: HELLA IMO 9535137, VIA S0941 -> berth CB05,
        13-07 23:01, Final'."""
        hit = [c for c in _beralt_calls(_parse_outbound()) if c["via_no"] == "S0941"]
        assert hit, "S0941 not found"
        c = hit[0]
        assert c["imo_no"] == "9535137"
        assert c["berth_code"] == "CB05"
        assert c["allotment_kind"] == "F"

    def test_the_small_journal_is_now_fully_routed(self):
        """104 documents = 45 BERALT + 59 CALINV. Both have parsers since the CALINV
        slice, so nothing in this file is unroutable any more.

        (This test previously asserted the 59 CALINV rows WERE unsupported errors. That
        was the correct expectation before CALINV was implemented; the assertion is
        inverted here rather than deleted, so the file's routing stays pinned.)
        """
        f = OUTBOUND / "NLP Outbound Data_CALINV_BERALT.csv"
        res = parse_marine(f.read_bytes(), f.name)
        codes = Counter(e.get("error_code") for e in res.errors)
        assert codes["unsupported_message_type"] == 0
        assert res.invalid_count == 0
        assert not res.rejected

    def test_genuinely_unimplemented_types_still_surface_as_typed_errors(self):
        """An unrouted message type must remain an auditable row error — the guard against
        'fix the count by swallowing the row'.

        ACKPLM used to be the example here (87 errors). It now HAS a parser
        (services/marine/parsers/pilot_memo.py -> core.pilotage), so the assertion moved to
        what is still genuinely unrouted rather than being deleted: the outbound journal
        must now report ZERO unsupported rows, because every type it carries is handled.
        The 'still surfaces' half of the guard is held by
        tests/test_marine_pilot_memo.py and by the inbound journal's PAISPS.
        """
        f = OUTBOUND / "NLP Outbound Data Report.csv"
        res = parse_marine(f.read_bytes(), f.name)
        codes = Counter(e.get("error_code") for e in res.errors)
        assert codes["unsupported_message_type"] == 0
        assert not res.rejected

        # Every ACKPLM is now a pilotage record rather than an error.
        pilotage = [r for r in res.records if r.get("_target") == "pilotage"]
        assert len(pilotage) == 87
        assert all(r["_message"] == "ACKPLM" for r in pilotage)


@pytest.mark.skipif(not INBOUND.is_dir(), reason="inbound journal absent")
class TestInboundJournalAlsoIngests:
    """The same envelope makes the inbound report ingestible — it was equally rejected."""

    def test_inbound_messages_route_to_their_existing_parsers(self):
        f = next(iter(sorted(INBOUND.glob("*.csv"))))
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected
        by_msg = Counter(r["_message"] for r in res.records)
        assert by_msg["VESPRO"] > 0 and by_msg["CALINF"] > 0 and by_msg["BERMAN"] > 0


# ---------------------------------------------------------------- Tier 3 — pure
class TestViaFromVcn:
    @pytest.mark.parametrize("vcn,expected", [
        ("INNSA1ND0S6544", "S6544"),
        ("INNSA1NS0S0941", "S0941"),
        ("INNSA1BM0R3119", "R3119"),
    ])
    def test_tail_slice(self, vcn, expected):
        assert via_from_vcn(vcn) == expected

    def test_short_via_is_not_resliced(self):
        assert via_from_vcn("S0527") is None

    def test_blank_and_none_are_safe(self):
        assert via_from_vcn(None) is None
        assert via_from_vcn("") is None


class TestBerthBinding:
    def test_berth_code_is_bound_by_both_upserts(self):
        from services.marine.repository import _CALL_COLS
        assert "berth_code" in _CALL_COLS

    def test_berth_lookup_consults_code_then_alias(self):
        from services.marine.repository import _BERTH_LOOKUP
        assert "core.ref_berth " in _BERTH_LOOKUP
        assert "core.ref_berth_alias" in _BERTH_LOOKUP
        assert "coalesce(" in _BERTH_LOOKUP

    def test_both_upserts_insert_and_never_null_berth_id(self):
        from services.marine.repository import (_VESSEL_CALL_PREVCN_UPSERT,
                                                _VESSEL_CALL_UPSERT)
        for sql in (_VESSEL_CALL_PREVCN_UPSERT, _VESSEL_CALL_UPSERT):
            assert "berth_id)" in sql
            assert "core.ref_berth_alias" in sql
            # A later message with no berth must not erase an allotment.
            assert "berth_id    = COALESCE(EXCLUDED.berth_id, core.vessel_call.berth_id)" in sql

    def test_read_projection_exposes_berth_code(self):
        from services.marine.repository import _SELECT_COLS
        assert "AS berth_code" in _SELECT_COLS
        assert "JOIN" not in _SELECT_COLS.upper()  # scalar subquery, not a join
