"""Customs parser tests — exercise every parser against the OFFICIAL JNPA customer
files (module 5). No synthetic fixtures: the samples themselves are the fixtures.

The suite is skipped when the customer data directory is absent (e.g. CI without
the ~8 MB customer corpus), so it never blocks a build; run locally with the data
present (or CUSTOMS_DATA_DIR set) for full coverage.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from services.customs.parsers import (
    CustomsParseError,
    parse_chpoi03,
    parse_chpoi10,
    parse_chpoi13,
    parse_leo_xlsx,
    parse_rms_txt,
    parse_shipping_bill_xlsx,
)

DATA_DIR = Path(os.environ.get(
    "CUSTOMS_DATA_DIR",
    os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs"),
))

pytestmark = pytest.mark.skipif(
    not DATA_DIR.is_dir(),
    reason=f"customer customs data dir not present: {DATA_DIR}",
)


def _files(sub: str, pattern: str) -> list[Path]:
    return sorted((DATA_DIR / sub).glob(pattern))


# --------------------------------------------------------------------------- IGM
def test_igm_parses_all_files_with_expected_counts():
    # (igm_no, cargo_lines, containers) measured directly from the customer files.
    expected = {
        "1193612": (1189, 2794),
        "1194564": (1, 362),
        "1194792": (30, 95),
        "1195489": (377, 1106),
    }
    seen = {}
    for f in _files("IGM", "CHPOI03_*.xml"):
        pm = parse_chpoi03(str(f))
        assert pm.message["message_type"] == "CHPOI03"
        assert pm.message["module"] == "IGM"
        assert len(pm.payload["vessels"]) == 1
        v = pm.payload["vessels"][0]
        lines = len(v["lines"])
        seen[v["igm_no"]] = (lines, pm.record_count)
        # container count == sum of per-line containers
        assert pm.record_count == sum(len(ln["containers"]) for ln in v["lines"])
    for igm, exp in expected.items():
        assert seen.get(igm) == exp, f"IGM {igm}: got {seen.get(igm)} want {exp}"


def test_igm_container_and_header_fields():
    f = _files("IGM", "CHPOI03_1194792_*.xml")[0]
    pm = parse_chpoi03(str(f))
    v = pm.payload["vessels"][0]
    assert v["igm_no"] == "1194792"
    assert v["igm_date"] == dt.date(2026, 5, 22)
    assert v["shipping_line_code"] == "HPL"
    line = v["lines"][0]
    assert line["line_no"] == 1
    assert line["importer_name"]
    c = line["containers"][0]
    assert c["container_no"] == "CAIU6709422"
    assert c["seal_no"] == "HLG5397947"
    assert c["iso_size_type"] == "2200"
    assert c["container_status"] == "FCL"
    assert isinstance(c["iso_valid"], bool)


# --------------------------------------------------------------------------- OOC
def test_ooc_parses_all_files():
    expected_items = {"9190401": 2, "9259230": 6, "9351819": 2, "9352934": 1}
    for f in _files("OOC", "CHPOI10_*.xml"):
        pm = parse_chpoi10(str(f))
        assert pm.message["module"] == "OOC"
        assert len(pm.payload["oocs"]) == 1
        o = pm.payload["oocs"][0]
        assert o["bill_of_entry_no"]
        assert o["out_of_charge_no"]
        items = sum(len(c["items"]) for c in o["containers"])
        assert items == expected_items[o["bill_of_entry_no"]]


def test_ooc_nested_item_fields():
    f = _files("OOC", "CHPOI10_9352934_*.xml")[0]
    o = parse_chpoi10(str(f)).payload["oocs"][0]
    assert o["igm_no"] == "1194193"
    assert o["out_of_charge_date"] == dt.date(2026, 6, 6)
    cont = o["containers"][0]
    assert cont["container_no"] == "EOLU8617280"
    item = cont["items"][0]
    assert item["hs_classification"] == "34024900"
    assert item["cif_value"] == pytest.approx(6892414.78)


# -------------------------------------------------------------------------- SMTP
def test_smtp_parses_all_files_with_expected_line_counts():
    expected = {"2697411": 45, "2697412": 9, "2697413": 95,
                "2697414": 6, "2697415": 7, "2697416": 47}
    for f in _files("SMTP", "CHPOI13_*.xml"):
        pm = parse_chpoi13(str(f))
        assert pm.message["module"] == "SMTP"
        p = pm.payload["permits"][0]
        assert pm.record_count == len(p["lines"])
        assert expected[p["smtp_no"]] == pm.record_count
        # one bond/destination per permit
        assert p["bond_no"] and p["destination_code"]


def test_smtp_line_fields():
    f = _files("SMTP", "CHPOI13_2697414_*.xml")[0]
    p = parse_chpoi13(str(f)).payload["permits"][0]
    assert p["smtp_no"] == "2697414"
    assert p["bond_no"] == "2000067135"
    assert p["destination_code"] == "INBNG6"
    line = p["lines"][0]
    assert line["container_no"] == "MAGU2317811"
    assert line["seal_no"] == "FX35782225"


# --------------------------------------------------------------------------- RMS
def test_rms_parses_selection_and_empty_lists():
    results = {f.name: parse_rms_txt(str(f)) for f in _files("RMS", "*.txt")}
    # 3.txt explicitly selects no containers
    empty = results["3.txt"]
    assert empty.payload["scanlist"]["any_selected"] is False
    assert empty.record_count == 0
    assert empty.payload["containers"] == []
    # 1.txt selects 16
    one = results["1.txt"]
    assert one.record_count == 16
    assert one.payload["scanlist"]["igm_no"] == "1191409"
    assert one.payload["scanlist"]["vessel_name"] == "AL RAWDAH"
    c0 = one.payload["containers"][0]
    assert c0["container_no"] == "BWLU9101815"
    assert c0["scan_machine"] == "D"
    assert c0["scan_location"] == "INNSA1RSDT02"
    assert c0["sl_no"] == 1


# --------------------------------------------------------------------- LEO / SB
def test_leo_and_sb_xlsx():
    leo = parse_leo_xlsx(str((DATA_DIR / "LEO" / "leodetails.xlsx")))
    assert leo.message["module"] == "LEO"
    assert leo.record_count == 100
    r0 = leo.payload["rows"][0]
    assert r0["sb_no"] == "2343823"
    assert r0["sb_date"] == dt.date(2026, 4, 13)
    assert r0["leo_date"] == dt.date(2026, 4, 14)
    assert r0["rotation_no"] == "1180983"

    sb = parse_shipping_bill_xlsx(str((DATA_DIR / "Shipping Bill" / "shippingbill.xlsx")))
    assert sb.message["module"] == "SHIPPING_BILL"
    assert sb.record_count == 100
    s0 = sb.payload["rows"][0]
    assert s0["sb_no"] == "4014226"
    assert s0["sb_date"] == dt.date(2026, 6, 10)
    assert s0["site_id"] == "INJNP1"


# ---------------------------------------------------------------- error handling
def test_wrong_root_raises(tmp_path):
    bad = tmp_path / "bad.xml"
    bad.write_text("<?xml version='1.0'?><NotAPayload><x/></NotAPayload>")
    with pytest.raises(CustomsParseError):
        parse_chpoi03(str(bad))
    with pytest.raises(CustomsParseError):
        parse_chpoi10(str(bad))
    with pytest.raises(CustomsParseError):
        parse_chpoi13(str(bad))
