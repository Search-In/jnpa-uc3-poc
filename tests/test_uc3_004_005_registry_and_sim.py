"""UC3-004 (vehicle->transporter registry) and UC3-005 (corridor simulation).

The valuable properties here are the ones that keep generated data honest, so
that is what these tests pin:

  * a SYNTHETIC mapping always carries its assumption reference and seed;
  * a transporter name is never guessed — an ambiguous or unmatched name resolves
    to None rather than to "probably this one";
  * the simulation is deterministic (same seed => identical population and hash)
    and its IN/OUT split reproduces the published anchor ratio exactly;
  * every simulated row is labelled.

Everything below is pure — no database, no clock, no network — so it runs in CI.
The live-DB assertions live behind UC3_TEST_DSN like the other UC3 suites.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


seed004 = _load("seed_uc3_004", "scripts/seed_uc3_004_vehicle_registry.py")
seed005 = _load("seed_uc3_005", "scripts/seed_uc3_005_corridor_simulation.py")

from services.vehicle_registry.repository import norm_plate  # noqa: E402
from services.vehicle_registry.service import ASSUMPTION_TEXT, _shape  # noqa: E402


# ---------------------------------------------------------------- UC3-004 ----
def test_contract_plates_are_the_ten_from_the_ticket():
    assert len(seed004.CONTRACT_PLATES) == 10
    assert "MH43BX1488" in seed004.CONTRACT_PLATES


@pytest.mark.parametrize("raw,expected", [
    ("mh43 bx1488", "MH43BX1488"), ("MH-43-BX-1488", "MH43BX1488"), ("", ""),
])
def test_plate_normalisation(raw, expected):
    assert norm_plate(raw) == expected


def test_truncated_name_resolves_to_the_single_matching_company():
    """The corpus prints 'TRANSTA' for TRANSTAR HANDLING & WAREHOUSING CO."""
    companies = [(1855, "TRANSTAR HANDLING & WAREHOUSING CO"), (2, "OTHER CO")]
    assert seed004.resolve_company("TRANSTA", companies) == 1855
    assert seed004.resolve_company("Transtar Handling & Warehousing Co", companies) == 1855


def test_ambiguous_prefix_is_never_guessed():
    companies = [(1, "TRANS ALPHA"), (2, "TRANS BETA")]
    assert seed004.resolve_company("TRANS", companies) is None


def test_unknown_name_resolves_to_none():
    assert seed004.resolve_company("NOT A COMPANY", [(1, "ACME")]) is None
    assert seed004.resolve_company("", [(1, "ACME")]) is None


def test_synthetic_pick_is_deterministic_and_in_range():
    ids = [10, 20, 30, 40]
    first = [seed004.synthetic_pick(p, ids) for p in seed004.CONTRACT_PLATES]
    again = [seed004.synthetic_pick(p, ids) for p in seed004.CONTRACT_PLATES]
    assert first == again
    assert all(v in ids for v in first)


def test_synthetic_rows_leave_the_service_labelled():
    shaped = _shape({"provenance": "SYNTHETIC", "assumption_ref": "A-G6"})
    assert shaped["is_synthetic"] is True
    assert shaped["seed"] == "UC3-004:A-G6:v1"
    assert shaped["assumption_text"] == ASSUMPTION_TEXT


def test_evidenced_rows_carry_no_assumption_noise():
    shaped = _shape({"provenance": "DOCUMENT_EVIDENCED", "source_ref": "eir1_psa_bmct"})
    assert shaped["is_synthetic"] is False
    assert shaped["seed"] is None and shaped["assumption_text"] is None


# ---------------------------------------------------------------- UC3-005 ----
def test_simulation_is_twenty_thousand_trucks_over_thirteen_segments():
    trucks = seed005.build_trucks()
    assert len(trucks) == 20_000
    segs = {t["segment_code"] for t in trucks}
    assert segs == {f"SEG-{i:02d}" for i in range(13)}


def test_in_out_split_reproduces_the_published_anchor_ratio_exactly():
    trucks = seed005.build_trucks()
    inbound = sum(1 for t in trucks if t["direction"] == "IN")
    expected = round(20_000 * seed005.ANCHOR_IN_TEU / seed005.ANCHOR_TOTAL_TEU)
    assert inbound == expected == 7_199
    assert seed005.ANCHOR_IN_TEU + seed005.ANCHOR_OUT_TEU == seed005.ANCHOR_TOTAL_TEU


def test_generation_is_deterministic():
    assert seed005.build_trucks() == seed005.build_trucks()


def test_config_hash_is_frozen_and_stable():
    assert seed005.config_sha256() == seed005.config_sha256()
    assert seed005.config_sha256() == (
        "832056719aac33e7e465e10dfb10aa3dc54ee423d23e72c32b337e0fd5c86775")


def test_truck_uids_are_unique_so_a_rerun_upserts_rather_than_duplicates():
    trucks = seed005.build_trucks()
    assert len({t["truck_uid"] for t in trucks}) == len(trucks)


def test_simulated_plates_cannot_collide_with_corpus_plates():
    """Corpus plates are MH..; generated ones are a reserved SIM....... series."""
    trucks = seed005.build_trucks()
    assert all(t["truck_no"].startswith("SIM") for t in trucks)


def test_calibration_window_covers_the_anchor_day():
    assert seed005.CALIBRATION_FROM <= seed005.ANCHOR_DATE <= seed005.CALIBRATION_TO
