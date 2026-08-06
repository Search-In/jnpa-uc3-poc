"""Non-finite numerics must never reach a numeric column or a JSON response.

`float()` ACCEPTS 'NaN', 'inf', 'Infinity' and an overflowing literal like '1e400' — it
does not raise — so a coercion helper that only catches ValueError lets them through.
PostgreSQL `numeric` then stores NaN happily, and the failure surfaces much later as
`ValueError: Out of range float values are not JSON compliant` during response
serialisation, taking out the whole page rather than the one bad cell.

This module is the regression guard for that path.
"""
from __future__ import annotations

import math

import pytest

from services.marine.parsers.pcs_common import to_num
from services.marine.parsers.sea_channel_shp import _num as shp_num

NON_FINITE = ["NaN", "nan", "NAN", "inf", "-inf", "Infinity", "-Infinity", "1e400", "-1e400"]


class TestPcsToNum:
    @pytest.mark.parametrize("raw", NON_FINITE)
    def test_non_finite_text_becomes_none(self, raw):
        assert to_num(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("13.20", 13.2), ("0", 0.0), ("-5.5", -5.5),
        ("1,234.5", 1234.5), ("  9.60  ", 9.6),
    ])
    def test_real_numbers_are_unchanged(self, raw, expected):
        """The fix must not alter a single valid measurement."""
        assert to_num(raw) == expected

    def test_zero_survives(self):
        """0 is a legitimate value, not an absence — it must not become None."""
        assert to_num("0") == 0.0

    @pytest.mark.parametrize("raw", ["abc", "", "   ", None])
    def test_unparseable_stays_none_as_before(self, raw):
        assert to_num(raw) is None

    def test_every_result_is_json_serialisable(self):
        import json
        for raw in NON_FINITE + ["13.20", "0", "abc", None]:
            json.dumps({"v": to_num(raw)}, allow_nan=False)  # raises if non-finite


class TestShapefileNum:
    @pytest.mark.parametrize("raw", NON_FINITE)
    def test_non_finite_becomes_none(self, raw):
        assert shp_num(raw) is None

    def test_real_numbers_are_unchanged(self):
        assert shp_num("15.4") == 15.4
        assert shp_num(15.4) == 15.4

    def test_float_nan_object_is_rejected_too(self):
        """Not only text: a float('nan') arriving from a shapefile field is also caught."""
        assert shp_num(float("nan")) is None
        assert shp_num(float("inf")) is None


class TestBathymetryHelperAlreadyGuarded:
    def test_the_existing_precedent_still_holds(self):
        """bathymetry_model._num had this guard first; the two fixed helpers now match it,
        so all three agree on what a non-measurement is."""
        from services.marine.parsers.bathymetry_model import _num as bathy_num
        for raw in ("NaN", "inf", float("nan"), float("inf")):
            assert bathy_num(raw) is None
        assert bathy_num("12.5") == 12.5


def test_all_three_helpers_agree():
    """One rule, three call sites — a future edit to any one of them breaks this."""
    from services.marine.parsers.bathymetry_model import _num as bathy_num
    for raw in NON_FINITE:
        results = [to_num(raw), shp_num(raw), bathy_num(raw)]
        assert results == [None, None, None], f"{raw!r} -> {results}"


def test_finite_guard_is_the_mechanism():
    """Documents WHY: float() accepts these, so catching ValueError alone is not enough."""
    for raw in NON_FINITE:
        parsed = float(raw)          # does NOT raise
        assert not math.isfinite(parsed)
