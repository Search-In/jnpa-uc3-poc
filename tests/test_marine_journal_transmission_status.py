"""Failed PCS transmissions are QUARANTINED, not ingested (UC1-002).

A message journal records what was TRANSMITTED and how the PCS answered. A row can
therefore carry a perfectly well-formed message that the PCS REFUSED — the inbound corpus
journal has ten, every one a VESPRO rejected with "Object reference not set to an instance
of an object." Such a document describes a failed attempt, not a fact about the port.

Before this guard the REQUEST column was read and the STATUS column ignored, so those ten
became core.vessel upserts. Three of the five vessels involved appear ONLY in failed rows
(SAKIZAYA INTEGRITY 9780146, NEW ADEN 9912000, COLONEL S P WAHI 9576404), so the corpus
gained three vessels the PCS never accepted.

Tier 1 — pure helper (synthetic journals, no corpus).
Tier 2 — the corpus journals end to end.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.marine.parsers import parse_marine
from services.marine.parsers.envelope import (extract_xml_documents, journal_rows)

DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine")))
INBOUND = DATA_DIR / "Inbound_CALINF_BERMAN"
OUTBOUND = DATA_DIR / "Outbound_CALINV_BERALT"

corpus = pytest.mark.skipif(not INBOUND.is_dir(),
                            reason=f"inbound journal absent: {INBOUND}")

_HEADER = ("COMMON_REF_NO,IMO,VIA_NO,VOYAGE_NO,VESSEL_NAME,MESSAGE_TYPE,"
           "RESPONSE_DATE,REQUEST,RESPONSE,STATUS")
_DOC = ("<VesselProfile><DocumentHeader><DocumentType>VESPRO</DocumentType>"
        "</DocumentHeader><VesselProfileDetails><IMONumber>{imo}</IMONumber>"
        "<VesselName>{name}</VesselName></VesselProfileDetails></VesselProfile>")


def _journal(*rows: tuple[str, str, str]) -> bytes:
    """Synthesise a journal: (imo, vessel_name, status) per row."""
    lines = ["NLP Inbound Data Report", "From Date: ''", "To Date: ''",
             "VIA No.: ''", "IMO No.: ''", "", _HEADER]
    for imo, name, status in rows:
        doc = _DOC.format(imo=imo, name=name).replace('"', '""')
        lines.append(f'ref{imo},{imo},,,{name},Vespro,01/01/2026 00:00,"{doc}",,{status}')
    return ("\n".join(lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------- Tier 1 — pure
class TestJournalVerdict:
    def test_failed_row_is_quarantined_and_not_parsed(self):
        res = parse_marine(_journal(("9111111", "GOOD SHIP", "Success"),
                                    ("9222222", "GHOST SHIP", "Failed")), "j.csv")
        imos = {r.get("imo_no") for r in res.records}
        assert "9111111" in imos
        assert "9222222" not in imos, "a refused transmission became a vessel"

        failed = [e for e in res.errors if e["error_code"] == "transmission_failed"]
        assert len(failed) == 1
        assert "GHOST SHIP" in failed[0]["error_detail"]
        assert failed[0]["raw_value"] == "Failed"
        assert res.invalid_count == 1

    def test_row_count_still_counts_every_document(self):
        """`summary.rows` must keep meaning "documents this file carries" — the response
        contract is unchanged, only what is PERSISTED differs."""
        res = parse_marine(_journal(("9111111", "A", "Success"),
                                    ("9222222", "B", "Failed")), "j.csv")
        assert res.row_count == 2

    @pytest.mark.parametrize("status", ["Failed", "failed", "FAILURE", "Error", "rejected"])
    def test_negative_verdicts_are_recognised_case_insensitively(self, status):
        res = parse_marine(_journal(("9222222", "B", status)), "j.csv")
        assert res.records == []
        assert any(e["error_code"] == "transmission_failed" for e in res.errors)

    @pytest.mark.parametrize("status", ["", "Success", "Queued", "In Progress", "??"])
    def test_only_an_EXPLICIT_negative_discards_a_row(self, status):
        """A blank or unfamiliar status must still ingest. Over-ingesting one row is
        recoverable; silently dropping client data is not."""
        res = parse_marine(_journal(("9111111", "A", status)), "j.csv")
        assert [r.get("imo_no") for r in res.records if r.get("_target") == "vessel"] \
            == ["9111111"]
        assert not [e for e in res.errors if e["error_code"] == "transmission_failed"]

    def test_an_all_failed_journal_reports_the_real_reason(self):
        """Not "no PCS XML found" — that would hide the finding behind a read error."""
        res = parse_marine(_journal(("9222222", "B", "Failed")), "j.csv")
        assert res.rejected
        codes = {e["error_code"] for e in res.errors}
        assert codes == {"transmission_failed"}
        assert "no_documents" not in codes

    def test_extraction_contract_is_unchanged(self):
        """extract_xml_documents answers "what does this file contain", so it still
        returns EVERY document. The verdict is the caller's business."""
        content = _journal(("9111111", "A", "Success"), ("9222222", "B", "Failed"))
        assert len(extract_xml_documents("JOURNAL", content)) == 2
        rows = journal_rows(content.decode("utf-8"))
        assert [r.accepted for r in rows] == [True, False]
        assert rows[1].source_row == 2 and rows[1].vessel_name == "B"


# ---------------------------------------------------------------- Tier 2 — corpus
@corpus
class TestCorpusJournals:
    def test_the_ten_failed_inbound_transmissions_are_quarantined(self):
        f = INBOUND / "NLP Inbound Data Report.csv"
        res = parse_marine(f.read_bytes(), f.name)
        failed = [e for e in res.errors if e["error_code"] == "transmission_failed"]
        assert len(failed) == 10, "corpus has exactly ten STATUS=Failed rows"
        assert all("Object reference not set" in e["error_detail"] for e in failed)

    def test_failed_only_vessels_never_enter_the_corpus(self):
        """These three IMOs appear ONLY in refused transmissions, so no parser output
        anywhere in the corpus may claim them."""
        failed_only = {"9780146", "9912000", "9576404"}
        found = set()
        for sub in ("VESPRO", "CALINF", "BERMAN", "VESARR", "VESDEP",
                    "Inbound_CALINF_BERMAN", "Outbound_CALINV_BERALT"):
            d = DATA_DIR / sub
            if not d.is_dir():
                continue
            for p in sorted(d.rglob("*")):
                if not p.is_file() or p.suffix.lower() not in (".xml", ".log", ".csv"):
                    continue
                for r in parse_marine(p.read_bytes(), p.name).records:
                    if r.get("_target") == "vessel" and r.get("imo_no") in failed_only:
                        found.add((r["imo_no"], p.name))
        assert not found, f"refused vessel profiles were ingested: {sorted(found)}"

    def test_outbound_journals_are_untouched(self):
        """Every outbound row carries RespCde 1; nothing there may be quarantined."""
        for f in sorted(OUTBOUND.glob("*.csv")):
            res = parse_marine(f.read_bytes(), f.name)
            assert not [e for e in res.errors if e["error_code"] == "transmission_failed"]
            assert res.records
