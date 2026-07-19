"""Customs service tests — format detection (pure, always runs) + a data-gated
directory-import smoke over the real customer files.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from services.customs.service import (
    UnknownCustomsFormat,
    detect_parser,
)

DATA_DIR = Path(os.environ.get(
    "CUSTOMS_DATA_DIR", os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs")))


def test_detect_parser_by_filename():
    # These formats are resolved by filename alone (no file access needed).
    assert detect_parser("/x/CHPOI03_1193612.xml")[1] == "IGM"
    assert detect_parser("/x/CHPOI10_9190401.xml")[1] == "OOC"
    assert detect_parser("/x/CHPOI13_2697411.xml")[1] == "SMTP"
    assert detect_parser("/x/RMS/1.txt")[1] == "RMS"


def test_detect_parser_rejects_unknown():
    with pytest.raises(UnknownCustomsFormat):
        detect_parser("/x/random.pdf")
    with pytest.raises(UnknownCustomsFormat):
        detect_parser("/x/notes.csv")


@pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}")
def test_detect_xlsx_leo_vs_shipping_bill():
    # xlsx disambiguation reads the header row of the real files.
    _, leo_mod = detect_parser(str(DATA_DIR / "LEO" / "leodetails.xlsx"))
    _, sb_mod = detect_parser(str(DATA_DIR / "Shipping Bill" / "shippingbill.xlsx"))
    assert leo_mod == "LEO"
    assert sb_mod == "SHIPPING_BILL"


def test_derive_workflow_stages():
    from services.customs.service import CustomsService as CS
    assert CS._derive_workflow({"status": {"ooc_cleared": True, "declared_igm": True}})["import_stage"] == "OUT_OF_CHARGE"
    assert CS._derive_workflow({"status": {"rms_selected": True, "declared_igm": True}})["import_stage"] == "SCAN_SELECTED"
    assert CS._derive_workflow({"status": {"declared_igm": True}})["import_stage"] == "MANIFESTED"
    assert CS._derive_workflow({"status": {"smtp_bonded": True}})["transhipment"] == "BONDED"
    assert CS._derive_workflow({"status": {"ooc_cleared": True}})["cleared_for_release"] is True
    assert CS._derive_workflow({"status": None})["import_stage"] is None


@pytest.mark.skipif(not DATA_DIR.is_dir(), reason=f"customs data dir absent: {DATA_DIR}")
def test_discover_finds_all_customer_files():
    from services.customs.service import CustomsService
    files = CustomsService._discover(str(DATA_DIR))
    names = {os.path.basename(f) for f in files}
    # 4 IGM + 4 OOC + 6 SMTP + 4 RMS + 1 LEO + 1 SB = 20 (dotfiles like .DS_Store excluded)
    assert len(files) == 20
    assert "shippingbill.xlsx" in names and "leodetails.xlsx" in names
    assert not any(n.startswith(".") for n in names)
