"""GAP-FLOW-05 — the free-day clock, and what it refuses to compute.

F-05 asked for a CHARGE clock. This is not one, and the tests pin why:

  * Not one file in the 449 carries a demurrage or detention RATE, so any rupee
    figure would be invented.
  * No document states when free time commences — discharge, entry inwards and
    out-of-charge each give a different answer.

What the corpus does carry is the allowance, typed into the IGM goods
description. Extracting a number out of prose is the risky part, so most of
these cases are about the extraction refusing to read the wrong number.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

_spec = importlib.util.spec_from_file_location(
    "extract_free_time", REPO_ROOT / "scripts" / "extract_free_time.py")
eft = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eft)


@pytest.mark.parametrize("desc,want", [
    # The three phrasings actually present in the corpus.
    ("HSCODE 25084000 14 FREE DAYS AT POD * CIN: U51900MH1998PTC115640", 14),
    ("SHIPPED ON BOARD 14 DAYS FREE TIME COMBINED DEMURRAGE AND DETENTION", 14),
    ("PET G5801 HS CODE: 3907 61 90 14 DAYS FREE TIME AT DESTINATION PORT", 14),
    ("PFFHOLNSLHB15.0076 21 DAYS FREE TIME HS CODE: 5503 20 00", 21),
    ("WOOD IN CHIPS FREIGHT PREPAID 04 DAYS FREE TIME AT PORT OF DISCHARGE", 4),
])
def test_reads_the_stated_allowance(desc, want):
    got = eft.extract(desc)
    assert got is not None and got[0] == want


def test_does_not_read_a_hazard_class_as_the_allowance():
    """The bug this ordering exists to prevent.

    On "UN.1247 CLASS 3 FREE TIME 14 DAYS" an earlier pattern order read **3** —
    the hazard class — because it sat immediately before "FREE TIME". A goods
    description is dense with numbers that fall next to the word by accident:
    HS codes, UN numbers, class digits, quantities.
    """
    got = eft.extract("HS CODE: 29161400 UN.1247 CLASS 3 FREE TIME 14 DAYS AT DESTINATION")
    assert got is not None
    assert got[0] == 14, f"read {got[0]} — the hazard class, not the allowance"


def test_a_term_with_no_figure_is_not_a_term():
    assert eft.extract("FREE DAYS AS PER LINER TARIFF") is None
    assert eft.extract("GENERAL CARGO HS CODE 4707.9000") is None
    assert eft.extract("") is None
    assert eft.extract(None) is None


def test_implausible_readings_are_rejected():
    """A three-digit 'allowance' is a mis-parse of a code, not a commercial term."""
    assert eft.extract("HS CODE 250840 999 DAYS FREE") is None


def test_the_evidence_phrase_is_kept():
    """The number alone is uncheckable. Storing the phrase it came from is what
    lets a reader confirm the extraction rather than trust it."""
    got = eft.extract("SHIPPED ON BOARD 14 DAYS FREE TIME COMBINED DEMURRAGE AND DETENTION")
    assert got is not None
    assert "FREE" in got[1].upper() and "14" in got[1]


def test_no_charge_is_computed_anywhere():
    """The clock reports days, never money — the corpus has no tariff."""
    src = (REPO_ROOT / "gateway" / "routers" / "free_time.py").read_text()
    assert '"charge_computed": False' in src
    for word in ("rate", "tariff", "amount_due", "rupee"):
        assert f"{word} *" not in src.lower()


def test_the_commencement_basis_is_recorded_not_assumed():
    """Whether free time runs from discharge, entry inwards or out-of-charge
    changes every figure, and no supplied document says which. The basis used is
    therefore stored per row so it can be recomputed."""
    ddl = (REPO_ROOT / "infra" / "postgres" / "v3" / "0143_container_free_time.sql").read_text()
    assert "commencement_basis" in ddl
    src = (REPO_ROOT / "gateway" / "routers" / "free_time.py").read_text()
    assert "commencement_basis" in src
