"""UC1-007 — empty / non-XML REQUEST cells must be quarantined, never silent-skipped."""
from __future__ import annotations

import csv
import io

from services.marine.parsers import parse_marine
from services.marine.parsers.envelope import iter_journal_rows

_CALINF_XML = (
    "<?xml version='1.0'?>"
    "<CallInformation>"
    "<DocumentType>CALINF</DocumentType>"
    "<CommonRefNumber>202606020001</CommonRefNumber>"
    "<VoyageDetails>"
    "<IMONumber>9939888</IMONumber>"
    "<VoyageNumber>IG2610W</VoyageNumber>"
    "<VesselName>TEST SHIP</VesselName>"
    "<EDTA>02032026 11:19</EDTA>"
    "</VoyageDetails>"
    "</CallInformation>"
)


def _journal_bytes() -> bytes:
    buf = io.StringIO()
    # Six banner rows so header lands at index 6 (corpus shape).
    for _ in range(6):
        buf.write("banner,,,,,,,\n")
    w = csv.writer(buf)
    w.writerow([
        "COMMON_REF_NO", "IMO_NUMBER", "VIA_NO", "VOYAGE_NO", "VESSEL_NAME",
        "MESSAGE_TYPE", "RESPONSE_DATE", "REQUEST", "RESPONSE",
    ])
    w.writerow([
        "202606020001", "9939888", "S1001", "IG2610W", "TEST SHIP",
        "CALINF", "02/03/2026", _CALINF_XML, "",
    ])
    w.writerow([
        "2.02606E+15", "", "", "", "",
        "VESARR", "02/03/2026", "", "",
    ])
    w.writerow([
        "202606020003", "1111111", "S1002", "XX0001", "OTHER",
        "BERMAN", "02/03/2026", "NOT_XML_AT_ALL", "",
    ])
    return buf.getvalue().encode("utf-8")


def test_iter_journal_rows_keeps_failed_transmissions():
    rows = iter_journal_rows(_journal_bytes().decode("utf-8"))
    assert len(rows) == 3
    assert rows[0]["skip_reason"] is None and rows[0]["xml"]
    assert rows[1]["skip_reason"] == "empty_request"
    assert rows[1]["message_type"] == "VESARR"
    assert "E+" in (rows[1]["common_ref_no"] or "").upper()
    assert rows[2]["skip_reason"] == "no_xml"


def test_parse_marine_quarantines_empty_request():
    res = parse_marine(_journal_bytes(), "NLP Inbound Data Report.csv")
    assert res.row_count == 3
    assert res.invalid_count == 2
    codes = {e["error_code"] for e in res.errors}
    assert "empty_request" in codes
    assert "no_xml" in codes
    empty = next(e for e in res.errors if e["error_code"] == "empty_request")
    assert empty["raw_value"] and "E+" in empty["raw_value"].upper()
    assert any(r.get("_message") == "CALINF" for r in res.records)
    assert any(r.get("voyage_no") == "IG2610W" for r in res.records)
