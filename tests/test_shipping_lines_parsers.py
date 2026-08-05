"""Unit tests for the shipping-line parsers + normalisers.

The pure normalisers (weight/enum/container) are tested directly. The end-to-end
file parsers are exercised against the REAL customer files when they are present on
disk (no fabricated data); if the data dir is absent (e.g. CI), those cases skip.
"""
from __future__ import annotations

import os

import pytest

from services.shipping_lines.parsers.common import (
    norm_category,
    norm_container,
    norm_freight_kind,
    norm_intish,
    resolve_weight,
)
from services.shipping_lines.parsers.column_maps import map_container_row
from services.shipping_lines.service import DEFAULT_DATA_DIR, ShippingLinesService, _parse, detect_format


# ----------------------------------------------------------------- pure normalisers
def test_freight_kind_mapping():
    assert norm_freight_kind("F") == "FULL"
    assert norm_freight_kind("E") == "EMPTY"      # E in a freight column == EMPTY (MTY)
    assert norm_freight_kind("MTY") == "EMPTY"
    assert norm_freight_kind("") == "UNKNOWN"


def test_category_mapping():
    assert norm_category("I") == "IMPORT"
    assert norm_category("E") == "EXPORT"          # E in a category column == EXPORT
    assert norm_category("T") == "TRANSHIP"
    assert norm_category(None) is None


def test_weight_inferred_by_magnitude_not_column_name():
    # BMCT mislabels a KG value as '...InMT' (19880) — magnitude wins -> KG, no *1000.
    kg, uom = resolve_weight({"grossweightinmt": "19880"})
    assert (kg, uom) == (19880.0, "KG")
    # APMT '...InMT' 20.95 is genuine tonnes -> *1000.
    kg, uom = resolve_weight({"grossweightinmt": "20.95"})
    assert (kg, uom) == (20950.0, "MT")
    # A genuine VGM in MT (9.238) -> 9238 kg.
    kg, uom = resolve_weight({"vgmweightinmt": "9.238"})
    assert (kg, uom) == (9238.0, "MT")


def test_container_and_intish():
    assert norm_container(" segu9719798 ") == "SEGU9719798"
    assert norm_intish("2210.0") == "2210"
    assert norm_intish("GEN") == "GEN"


def test_map_row_defaults_category_from_list_type():
    row = {"ContainerNbr": "SEGU9719798", "ISO": "4532", "Status": "F", "Line": "KMD",
           "POD": "MYPKG", "GrossWeightin KGS": "34010"}
    mapped = map_container_row(row, list_type="EAL", terminal="BMCT")
    assert mapped["container_no"] == "SEGU9719798"
    assert mapped["freight_kind"] == "FULL"
    assert mapped["category"] == "EXPORT"          # from EAL when no explicit category
    assert mapped["gross_weight_kg"] == 34010.0
    assert mapped["shipping_line_code"] == "KMD"
    assert mapped["raw"]["ISO"] == "4532"


def test_map_row_skips_blank_container():
    assert map_container_row({"ContainerNbr": ""}, list_type="IAL", terminal="APMT") is None


# ------------------------------------------------------- real-file end-to-end (skip if absent)
_HAVE_DATA = os.path.isdir(DEFAULT_DATA_DIR)
pytestmark_real = pytest.mark.skipif(not _HAVE_DATA, reason=f"no data dir: {DEFAULT_DATA_DIR}")


@pytestmark_real
def test_all_real_files_parse_with_sane_weights():
    files = ShippingLinesService._discover(DEFAULT_DATA_DIR)
    assert files, "expected customer files under the shipping-lines data dir"
    total = 0
    for path in files:
        list_type, terminal, _fmt = detect_format(path)
        parsed = _parse(path, list_type, terminal)
        total += parsed.record_count
        rows = parsed.containers or parsed.delivery_orders
        assert rows, f"no rows parsed from {path}"
        for c in parsed.containers:
            assert c["container_no"], "container_no must be present"
            if c["gross_weight_kg"] is not None:
                assert 200 <= c["gross_weight_kg"] <= 100000, (
                    f"implausible weight {c['gross_weight_kg']} in {path}")
    assert total > 5000, f"expected the full corpus, got {total} rows"


@pytestmark_real
def test_edo_parses_delivery_orders():
    edo = os.path.join(DEFAULT_DATA_DIR, "EDO", "EDO.xlsx")
    if not os.path.isfile(edo):
        pytest.skip("EDO.xlsx absent")
    parsed = _parse(edo, "EDO", "OTHER")
    assert parsed.delivery_orders
    do = parsed.delivery_orders[0]
    assert do["container_no"] and do["gate_pass_no"]
