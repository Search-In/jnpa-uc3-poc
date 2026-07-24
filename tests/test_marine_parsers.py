"""Marine PCS parser tests — exercise every parser against the OFFICIAL client
files in client-data/1-NLP Marine. No synthetic fixtures: the customer samples are
the fixtures (same posture as tests/test_customs_parsers.py).

Skipped when the data directory is absent (e.g. CI without the corpus), so it never
blocks a build; run locally with the data present (or MARINE_DATA_DIR set).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from services.marine.parsers import (
    detect_format,
    document_type,
    extract_xml_documents,
    parse_marine,
)
from services.marine.parsers.documents import safe_fromstring
from services.marine.parsers.pcs_common import parse_pcs_date, parse_pcs_dt, yn_bool

# client-data is a sibling of the jnpa-uc3-poc repo under "JNPA Code".
DATA_DIR = Path(os.environ.get(
    "MARINE_DATA_DIR",
    str(Path(__file__).resolve().parents[2] / "client-data" / "1-NLP Marine"),
))

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(),
    reason=f"marine client data dir not present: {DATA_DIR}",
)


def _files(sub: str, pattern: str) -> list[Path]:
    return sorted((DATA_DIR / sub).glob(pattern))


def _parse_all(sub: str, pattern: str):
    """Parse every file in a subfolder, collecting (file, ParseResult)."""
    out = []
    for f in _files(sub, pattern):
        out.append((f, parse_marine(f.read_bytes(), f.name)))
    return out


# ------------------------------------------------------------ pure datetime grammar
class TestPcsDatetime:
    def test_estimated_colon_form(self):
        assert parse_pcs_dt("11022026:17:00") == dt.datetime(2026, 2, 11, 17, 0, tzinfo=parse_pcs_dt("11022026:17:00").tzinfo)

    def test_actual_space_form(self):
        d = parse_pcs_dt("29072026 05:18")
        assert (d.year, d.month, d.day, d.hour, d.minute) == (2026, 7, 29, 5, 18)

    def test_issued_14_digit_form(self):
        d = parse_pcs_dt("15072026173448")
        assert (d.year, d.month, d.day, d.hour, d.minute, d.second) == (2026, 7, 15, 17, 34, 48)

    def test_date_only_and_junk(self):
        assert parse_pcs_date("28062026") == dt.date(2026, 6, 28)
        assert parse_pcs_dt("") is None
        assert parse_pcs_dt("not-a-date") is None

    def test_yn_bool_tristate(self):
        assert yn_bool("Y") is True
        assert yn_bool("N") is False
        assert yn_bool("") is None


# ------------------------------------------------------------ envelope detection
class TestEnvelope:
    def test_direct_xml_detected(self):
        f = _files("CALINF", "*.xml")[0]
        assert detect_format(f.name, f.read_bytes()) == "XML"

    def test_log_detected(self):
        f = _files("VESARR", "*.log")[0]
        assert detect_format(f.name, f.read_bytes()) == "LOG"

    def test_log_extracts_embedded_xml(self):
        f = _files("VESARR", "*.log")[0]
        docs = extract_xml_documents("LOG", f.read_bytes())
        assert docs and all(d.lstrip().startswith("<Vessel") for d in docs)

    def test_document_type_from_tag_and_root(self):
        f = _files("BERMAN", "*.xml")[0]
        root = safe_fromstring(f.read_bytes().decode("utf-8", "replace").lstrip("﻿"))
        assert document_type(root) == "BERMAN"


# ------------------------------------------------------------ VESPRO → vessel
def test_vespro_maps_to_vessel_master():
    results = _parse_all("VESPRO", "*.xml")
    assert results, "no VESPRO files found"
    for f, res in results:
        assert not res.rejected, f"{f.name} rejected"
        vessels = [r for r in res.records if r["_target"] == "vessel"]
        assert len(vessels) == 1, f"{f.name}: expected one vessel record"
        v = vessels[0]
        assert v["_message"] == "VESPRO"
        assert v["imo_no"] and v["imo_no"].isdigit(), f"{f.name}: bad imo {v['imo_no']!r}"
        assert v["vessel_name"], f"{f.name}: missing vessel name"
        # dimensions parse to numbers where present
        for k in ("loa_m", "beam_m", "grt"):
            assert v[k] is None or isinstance(v[k], float)
        assert isinstance(v["_insurance"], list)


# ------------------------------------------------------------ CALINF → vessel_call (pre-VCN)
def test_calinf_is_pre_vcn_call():
    results = _parse_all("CALINF", "*.xml")
    assert results, "no CALINF files found"
    for f, res in results:
        calls = [r for r in res.records if r["_target"] == "vessel_call"]
        assert len(calls) == 1, f"{f.name}: expected one call record"
        c = calls[0]
        assert c["_message"] == "CALINF"
        assert c["vcn"] is None, f"{f.name}: CALINF must have no VCN"
        assert c["voyage_no"] or c["imo_no"], f"{f.name}: no call key"
        assert c["eta"] is None or isinstance(c["eta"], dt.datetime)


# ------------------------------------------------------------ BERMAN → vessel_call (VCN)
def test_berman_carries_vcn():
    results = _parse_all("BERMAN", "*.xml")
    assert results, "no BERMAN files found"
    for f, res in results:
        calls = [r for r in res.records if r["_target"] == "vessel_call"]
        assert len(calls) == 1, f"{f.name}: expected one call record"
        c = calls[0]
        assert c["_message"] == "BERMAN"
        assert c["vcn"], f"{f.name}: BERMAN must carry a VCN"
        assert c["vcn"].startswith("INNSA"), f"{f.name}: unexpected VCN {c['vcn']!r}"


# ------------------------------------------------------------ VESARR / VESDEP → events
def test_vesarr_emits_actual_events():
    results = _parse_all("VESARR", "*.log")
    assert results, "no VESARR files found"
    total_events = 0
    for f, res in results:
        events = [r for r in res.records if r["_target"] == "vessel_call_event"]
        for e in events:
            assert e["_message"] == "VESARR"
            assert e["event_type"] in {"ANCHORED", "PILOT_BOARDED", "BERTHED", "ARRIVED"}
            assert isinstance(e["event_ts"], dt.datetime)
            assert e["vcn"], f"{f.name}: event without a resolution key"
        total_events += len(events)
    assert total_events > 0, "VESARR produced no events"


def test_vesdep_emits_departure_events():
    results = _parse_all("VESDEP", "*.log")
    assert results, "no VESDEP files found"
    seen_types: set[str] = set()
    for _f, res in results:
        for e in (r for r in res.records if r["_target"] == "vessel_call_event"):
            assert e["_message"] == "VESDEP"
            assert e["event_type"] in {"PILOT_BOARDED", "DEPARTED", "SAILED"}
            assert isinstance(e["event_ts"], dt.datetime)
            seen_types.add(e["event_type"])
    assert seen_types, "VESDEP produced no events"


# ------------------------------------------------------------ routing robustness
class TestRouting:
    def test_unsupported_message_is_a_typed_error_not_a_crash(self):
        res = parse_marine(b"<SomethingElse><DocumentType>WHATEVER</DocumentType></SomethingElse>", "x.xml")
        assert res.records == []
        assert any(e["error_code"] == "unsupported_message_type" for e in res.errors)

    def test_malformed_xml_is_rejected_gracefully(self):
        res = parse_marine(b"<VesselProfile><unclosed>", "x.xml")
        assert any(e["error_code"] in ("xml_parse_error", "parse_failed") for e in res.errors)

    def test_empty_file_rejected(self):
        res = parse_marine(b"", "x.xml")
        assert res.rejected
        assert any(e["error_code"] == "no_documents" for e in res.errors)

    def test_every_record_is_tagged(self):
        # Across all message types, no record ever leaves the framework untagged.
        for sub, pat in (("VESPRO", "*.xml"), ("CALINF", "*.xml"), ("BERMAN", "*.xml"),
                         ("VESARR", "*.log"), ("VESDEP", "*.log")):
            for _f, res in _parse_all(sub, pat):
                for r in res.records:
                    assert r.get("_target") in {"vessel", "vessel_call", "vessel_call_event"}
                    assert r.get("_message")
