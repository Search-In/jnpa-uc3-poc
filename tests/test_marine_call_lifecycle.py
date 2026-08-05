"""CALINF + BERMAN call-lifecycle tests — parser stage stamping + terminal resolution.

Covers the business behaviour added for the CALINF/BERMAN slice:
  * CALINF stamps the call 'Planned' and keeps the document's own <Status> separately;
  * BERMAN stamps 'Berth Planned' and derives the terminal from the VCN infix;
  * both carry a `terminal_code` the repository resolves to terminal_id.

Tier 1 runs against the OFFICIAL client files in client-data/1-NLP Marine (same posture as
tests/test_marine_parsers.py). Tier 2 is pure — the VCN decoder and the bound-column
whitelist need no corpus and no database.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.marine.parsers import parse_marine
from services.marine.parsers.pcs_common import (CALL_STATUS_BERTH_PLANNED,
                                                CALL_STATUS_PLANNED, terminal_from_vcn)

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))


def _calls(sub: str) -> list[dict]:
    """Every file in a subfolder → its vessel_call records."""
    out: list[dict] = []
    for f in sorted((DATA_DIR / sub).glob("*.xml")):
        res = parse_marine(f.read_bytes(), f.name)
        assert not res.rejected, f"{f.name} rejected"
        out.extend(r for r in res.records if r["_target"] == "vessel_call")
    return out


corpus = pytest.mark.skipif(
    not (DATA_DIR / "CALINF").is_dir(), reason=f"marine client data absent: {DATA_DIR}")


# ---------------------------------------------------------------- Tier 1 — CALINF
@corpus
class TestCalinfStage:
    def test_every_calinf_is_planned(self):
        calls = _calls("CALINF")
        assert len(calls) == 20, "expected the 20 corpus CALINF documents"
        assert all(c["status"] == CALL_STATUS_PLANNED for c in calls)

    def test_document_status_is_kept_not_overwritten(self):
        """CALINF <Status> is a PCS record code ('C'/'F'), not a lifecycle state. It must
        survive alongside the stamped stage rather than being silently replaced."""
        calls = _calls("CALINF")
        raw = {c["doc_status"] for c in calls if c["doc_status"]}
        assert raw, "no document status captured"
        assert raw <= {"C", "F"}, f"unexpected CALINF <Status> values: {raw}"

    def test_calinf_stays_pre_vcn(self):
        assert all(c["vcn"] is None for c in _calls("CALINF"))

    def test_eta_voyage_and_purpose_are_populated(self):
        for c in _calls("CALINF"):
            assert c["eta"] is not None, "EDTA should map to eta"
            assert c["voyage_no"], "VoyageNumber missing"
            assert c["purpose"], "PurposeOfvisit missing"

    def test_terminal_code_comes_from_dock_or_to_code(self):
        codes = {c["terminal_code"] for c in _calls("CALINF")}
        # INJNP1 is the PORT code — carried, but deliberately NOT aliased to a terminal.
        assert "INJNP1" in codes
        assert {"INNSA1NSI1", "INNSA1BMC1", "INNSA1JNP1"} <= codes


# ---------------------------------------------------------------- Tier 1 — BERMAN
@corpus
class TestBermanStage:
    def test_every_berman_is_berth_planned(self):
        calls = _calls("BERMAN")
        assert len(calls) == 14, "expected the 14 corpus BERMAN documents"
        assert all(c["status"] == CALL_STATUS_BERTH_PLANNED for c in calls)

    def test_vcn_and_rotation_are_assigned(self):
        for c in _calls("BERMAN"):
            assert c["vcn"], "VCN is the call key and must be present"
            assert c["rotation_no"], "RotationNumber missing"

    def test_terminal_is_derived_from_the_vcn_infix(self):
        """BERMAN has no DockORTOCode tag at all — without the VCN decode the terminal
        would be permanently NULL for every berth application."""
        calls = _calls("BERMAN")
        assert all(c["terminal_code"] for c in calls), "a BERMAN resolved no terminal"
        assert {c["terminal_code"] for c in calls} <= {"NSICT", "NSFT", "APMT", "NSIGT",
                                                       "BMCT", "NSDT"}

    def test_etb_is_mapped_where_present(self):
        """EDB appears on 5 of the 14 corpus documents — sparse, not absent."""
        etbs = [c["etb"] for c in _calls("BERMAN") if c["etb"] is not None]
        assert etbs, "EDB present in the corpus but nothing mapped to etb"

    def test_berth_is_not_invented(self):
        """BERMAN is the berth APPLICATION and carries no berth field; the allotment
        arrives with BERALT. Asserting the absence stops a future change from quietly
        fabricating one."""
        assert all(c.get("berth_code") is None for c in _calls("BERMAN"))


# ---------------------------------------------------------------- Tier 2 — pure
class TestTerminalFromVcn:
    @pytest.mark.parametrize("vcn,expected", [
        ("INNSA1BM0R3119", "BMCT"),
        ("INNSA1NS0R2893", "NSICT"),
        ("INNSA1NF0R2968", "NSFT"),
        ("INNSA1ND0R7325", "NSDT"),
        ("INNSA1GT0S0554", "APMT"),
        ("INNSA1NG0S0711", "NSIGT"),
    ])
    def test_known_infixes(self, vcn, expected):
        assert terminal_from_vcn(vcn) == expected

    def test_lowercase_vcn_still_resolves(self):
        assert terminal_from_vcn("innsa1bm0r3119") == "BMCT"

    def test_short_via_form_is_not_decoded(self):
        """'S0527' is a short VIA, not a VCN — decoding it would read random characters."""
        assert terminal_from_vcn("S0527") is None

    def test_unknown_infix_returns_none_rather_than_guessing(self):
        assert terminal_from_vcn("INNSA1ZZ0R3119") is None

    def test_blank_and_none_are_safe(self):
        assert terminal_from_vcn(None) is None
        assert terminal_from_vcn("") is None
        assert terminal_from_vcn("   ") is None


class TestCallBinding:
    def test_terminal_code_is_bound_by_both_upserts(self):
        from services.marine.repository import _CALL_COLS
        assert "terminal_code" in _CALL_COLS

    def test_terminal_lookup_consults_code_then_alias(self):
        """Resolve-or-NULL: canonical code first, alias second, never a stub row."""
        from services.marine.repository import _TERMINAL_LOOKUP
        assert "core.ref_terminal " in _TERMINAL_LOOKUP
        assert "core.ref_terminal_alias" in _TERMINAL_LOOKUP
        assert "coalesce(" in _TERMINAL_LOOKUP

    def test_both_upserts_insert_terminal_id(self):
        from services.marine.repository import (_VESSEL_CALL_PREVCN_UPSERT,
                                                _VESSEL_CALL_UPSERT)
        for sql in (_VESSEL_CALL_PREVCN_UPSERT, _VESSEL_CALL_UPSERT):
            assert "terminal_id" in sql
            assert "core.ref_terminal_alias" in sql, "alias fallback not wired"

    def test_terminal_id_is_never_downgraded_to_null_on_update(self):
        """A later message with no terminal must not erase one an earlier message set."""
        from services.marine.repository import (_VESSEL_CALL_PREVCN_UPSERT,
                                                _VESSEL_CALL_UPSERT)
        for sql in (_VESSEL_CALL_PREVCN_UPSERT, _VESSEL_CALL_UPSERT):
            assert "terminal_id = COALESCE(EXCLUDED.terminal_id, core.vessel_call.terminal_id)" in sql

    def test_read_projection_exposes_terminal_code(self):
        from services.marine.repository import _SELECT_COLS
        assert "AS terminal_code" in _SELECT_COLS
        # A scalar subquery, not a JOIN — row multiplicity must be unchanged.
        assert "JOIN" not in _SELECT_COLS.upper()
