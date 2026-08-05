"""ACKPLM / PLTMEM pilot-memo parser — the PCS-native producer for core.pilotage.

Tier 1 runs against the OFFICIAL client journals in client-data/1-NLP Marine (same posture
as tests/test_marine_parsers.py). Tier 2 is pure — the direction rule and the record shape
need no corpus and no database.

The invariant these tests exist to protect: this parser adds a PRODUCER, not a
correlation rule. It emits `via_no` from the VCN so the EXISTING _PILOTAGE_INSERT lookup
resolves call_id unchanged — no repository SQL, no API contract, no projection code.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from services.marine.parsers import REGISTRY, parse_marine
from services.marine.parsers.pilot_memo import (INWARD, OUTWARD, SHIFTING, parse_ackplm,
                                                parse_pltmem)

REPO = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))
OUTBOUND = DATA_DIR / "Outbound_CALINV_BERALT" / "NLP Outbound Data Report.csv"

corpus = pytest.mark.skipif(not OUTBOUND.is_file(),
                            reason=f"marine client data absent: {OUTBOUND}")

#: TSS AMBER — the golden runtime vessel. Its call carries a PILOT_BOARDED milestone and
#: had NO pilotage row before this parser existed.
AMBER_VCN = "INNSA1NF0S0776"


def _doc(details: str, root: str = "PilotMemoAcknowledgment",
         doc_type: str = "ACKPLM") -> ET.Element:
    """Minimal well-formed message with the given detail body."""
    container = ("PilotMemoAcknowledgmentDetails" if doc_type == "ACKPLM"
                 else "PilotMemoApplicationDetails")
    return ET.fromstring(
        f"<{root}><DocumentHeader><DocumentReference>"
        f"<DocumentType>{doc_type}</DocumentType>"
        f"<CommonRefNumber>REF1</CommonRefNumber></DocumentReference></DocumentHeader>"
        f"<DocumentDetails><{container}>{details}</{container}></DocumentDetails></{root}>")


# ------------------------------------------------------------------ Tier 1 — corpus
@corpus
class TestAgainstClientCorpus:
    @staticmethod
    def _pilotage():
        res = parse_marine(OUTBOUND.read_bytes(), OUTBOUND.name)
        return res, [r for r in res.records if r.get("_target") == "pilotage"]

    def test_every_ackplm_becomes_a_pilotage_record(self):
        _, rows = self._pilotage()
        assert len(rows) == 87, "expected the 87 corpus ACKPLM messages"
        assert all(r["_message"] == "ACKPLM" for r in rows)

    def test_ackplm_is_no_longer_unsupported(self):
        res, _ = self._pilotage()
        unsupported = {str(e.get("raw_value")) for e in res.errors
                       if e.get("error_code") == "unsupported_message_type"}
        assert "ACKPLM" not in unsupported

    def test_every_row_carries_the_correlation_key(self):
        """via_no is what the EXISTING _PILOTAGE_INSERT resolves call_id from."""
        _, rows = self._pilotage()
        assert all(r["via_no"] for r in rows)
        assert all(len(r["via_no"]) == 5 for r in rows)

    def test_rows_are_individually_identifiable(self):
        """row_sha256 is the ON CONFLICT key — a collision would silently drop a movement."""
        _, rows = self._pilotage()
        assert len({r["row_sha256"] for r in rows}) == len(rows)

    def test_pilot_name_is_preserved_even_though_the_code_is_unknown(self):
        _, rows = self._pilotage()
        named = [r for r in rows if r["extras"].get("pilot_name")]
        assert len(named) == 86, "86 of 87 corpus messages name a pilot"
        # The roster code is NOT invented from the name — see the module docstring.
        assert all(r["pilot_code"] is None for r in rows)

    def test_tss_amber_now_has_a_pilotage_record(self):
        """The whole point: the golden vessel had zero pilotage rows before this."""
        _, rows = self._pilotage()
        amber = [r for r in rows if r["extras"].get("vcn") == AMBER_VCN]
        assert len(amber) == 1, f"expected exactly one pilot memo for {AMBER_VCN}"
        r = amber[0]
        assert r["via_no"] == "S0776"          # resolves to call_id 48
        assert r["imo_no"] == "9241918"
        assert r["extras"]["pilot_name"] == "KULDEEP RAWAT"
        assert r["draft_fwd_m"] == 11.0 and r["draft_aft_m"] == 12.0
        assert r["pilot_boarded_at"] is not None


# ------------------------------------------------------------------ Tier 2 — pure
class TestMovementDirection:
    """Direction comes from operational facts, never from the opaque OperationType."""

    def test_boarding_at_a_berth_means_the_vessel_is_leaving(self):
        rec = parse_ackplm(_doc(
            "<VCN>INNSA1NF0S0776</VCN><PlaceOfPilotboarding>Berth</PlaceOfPilotboarding>"))[0]
        assert rec["movement_type"] == OUTWARD

    def test_movement_to_sea_is_outward(self):
        rec = parse_pltmem(_doc(
            "<VCN>INNSA1NF0S0776</VCN><VesselMovementTo>SEA</VesselMovementTo>",
            root="PilotMemoApplication", doc_type="PLTMEM"))[0]
        assert rec["movement_type"] == OUTWARD

    def test_berth_to_a_different_berth_is_shifting(self):
        rec = parse_pltmem(_doc(
            "<VCN>INNSA1NF0S0776</VCN><BerthFrom>BM4</BerthFrom><BerthTo>CB02</BerthTo>",
            root="PilotMemoApplication", doc_type="PLTMEM"))[0]
        assert rec["movement_type"] == SHIFTING

    def test_same_berth_echoed_is_not_a_shift(self):
        """The corpus echoes BerthFrom into BerthTo; that is not a berth-to-berth move."""
        rec = parse_pltmem(_doc(
            "<VCN>INNSA1NF0S0776</VCN><BerthFrom>BM4</BerthFrom><BerthTo>BM4</BerthTo>"
            "<VesselMovementTo>SEA</VesselMovementTo>",
            root="PilotMemoApplication", doc_type="PLTMEM"))[0]
        assert rec["movement_type"] == OUTWARD

    def test_boarding_anywhere_else_is_inward(self):
        rec = parse_ackplm(_doc(
            "<VCN>INNSA1NF0S0776</VCN>"
            "<PlaceOfPilotboarding>Pilot Station</PlaceOfPilotboarding>"))[0]
        assert rec["movement_type"] == INWARD

    def test_operation_type_is_never_used_as_a_direction(self):
        """'O' is constant on all 102 corpus messages, so it discriminates nothing.
        Two messages differing ONLY in OperationType must resolve the same way."""
        base = "<VCN>INNSA1NF0S0776</VCN><PlaceOfPilotboarding>Berth</PlaceOfPilotboarding>"
        a = parse_ackplm(_doc(base + "<OperationType>O</OperationType>"))[0]
        b = parse_ackplm(_doc(base + "<OperationType>I</OperationType>"))[0]
        assert a["movement_type"] == b["movement_type"] == OUTWARD

    def test_direction_is_always_a_legal_value(self):
        """core.pilotage.movement_type is NOT NULL CHECK IN (...) — a None would fail."""
        rec = parse_ackplm(_doc("<VCN>INNSA1NF0S0776</VCN>"))[0]
        assert rec["movement_type"] in (INWARD, OUTWARD, SHIFTING)


class TestRecordShape:
    def _rec(self):
        return parse_ackplm(_doc(
            "<VCN>INNSA1NF0S0776</VCN><IMONumber>9241918</IMONumber>"
            "<PilotName>KULDEEP RAWAT</PilotName>"
            "<PilotboardingDateTime>30062026:18:30</PilotboardingDateTime>"
            "<DateAndTimeOfSubmission>30062026:18:22</DateAndTimeOfSubmission>"
            "<DraftFwd>11.00</DraftFwd><DraftAft>12.00</DraftAft>"
            "<PlaceOfPilotboarding>Berth</PlaceOfPilotboarding>"))[0]

    def test_targets_pilotage_not_the_call_spine(self):
        rec = self._rec()
        assert rec["_target"] == "pilotage"
        assert rec["_message"] == "ACKPLM"

    def test_emits_every_column_the_repository_binds(self):
        """_pilotage_params reads these by name; a missing key would bind NULL silently."""
        rec = self._rec()
        for key in ("movement_type", "via_no", "imo_no", "vessel_name", "pilot_code",
                    "vessel_condition", "draft_fwd_m", "draft_aft_m", "pilot_boarded_at",
                    "first_line_at", "all_fast_at", "pilot_disembarked_at",
                    "berth_vacated_at", "anchor_down_at", "anchor_up_at", "submitted_at",
                    "row_sha256", "from_berth_code", "to_berth_code", "extras"):
            assert key in rec, f"missing bound column: {key}"

    def test_via_is_derived_from_the_vcn(self):
        assert self._rec()["via_no"] == "S0776"

    def test_a_malformed_vcn_yields_no_link_rather_than_a_wrong_one(self):
        rec = parse_ackplm(_doc("<VCN>S0776</VCN>"))[0]
        assert rec["via_no"] is None

    def test_missing_vcn_is_a_typed_parse_error(self):
        from services.marine.parsers.pcs_common import MarineParseError
        with pytest.raises(MarineParseError):
            parse_ackplm(_doc("<IMONumber>9241918</IMONumber>"))

    def test_application_request_time_is_not_written_as_an_actual(self):
        """PLTMEM's PilotRequiredDateAndTime is a REQUEST — writing it to
        pilot_boarded_at would fabricate a milestone that never happened."""
        rec = parse_pltmem(_doc(
            "<VCN>INNSA1NF0S0776</VCN>"
            "<PilotRequiredDateAndTime>30062026:18:30</PilotRequiredDateAndTime>",
            root="PilotMemoApplication", doc_type="PLTMEM"))[0]
        assert rec["pilot_boarded_at"] is None
        assert rec["extras"]["pilot_required_at"] == "30062026:18:30"


class TestNoArchitecturalDuplication:
    """This slice adds a PRODUCER. It must not add a second lifecycle or correlation rule."""

    def test_parser_derives_no_lifecycle_state(self):
        src = (REPO / "services/marine/parsers/pilot_memo.py").read_text(encoding="utf-8")
        for banned in ("derive_state", "MarineProjection", "pilot_status", "CallProjection"):
            assert banned not in src, f"parser must not touch the lifecycle layer: {banned}"

    def test_parser_owns_no_sql_and_no_db_access(self):
        src = (REPO / "services/marine/parsers/pilot_memo.py").read_text(encoding="utf-8")
        assert not re.search(r"(?:FROM|JOIN|INTO|UPDATE)\s+core\.", src, re.I)
        assert "get_engine" not in src and "sqlalchemy" not in src

    def test_repository_correlation_sql_is_unchanged(self):
        """The row links by the EXISTING VIA lookup — no new resolution path."""
        src = (REPO / "services/marine/repository.py").read_text(encoding="utf-8")
        assert "SELECT call_id FROM core.vessel_call WHERE via_no = :via_no" in src

    def test_pltmem_is_parsed_but_not_routed(self):
        """Routing it too would put two rows in a one-row-per-movement table."""
        assert "ACKPLM" in REGISTRY
        assert "PLTMEM" not in REGISTRY
        assert callable(parse_pltmem)

    def test_existing_message_routing_is_untouched(self):
        for msg in ("VESPRO", "CALINF", "CALINV", "BERMAN", "BERALT", "VESARR", "VESDEP"):
            assert msg in REGISTRY
