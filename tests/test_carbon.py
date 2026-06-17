"""Tests for the pure carbon-emissions calculator (carbon/, Appendix C #6).

These assert the calculation properties an evaluator relies on — linear scaling
in distance and idle minutes, the reefer > HGV ordering, and the AoI-rollup
conservation invariant — so the CO2e figure on the dashboard can never silently
drift from the documented factors. The pure functions are tested directly, so no
running server (and no infrastructure) is required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "carbon")):
    if p not in sys.path:
        sys.path.insert(0, p)

from carbon import calculator, factors  # noqa: E402


# --- (a) moving emissions scale linearly with distance ----------------------
def test_trip_emissions_scale_linearly_with_distance():
    e1 = calculator.trip_emissions_kg(10.0, 20.0, factors.HGV)
    e2 = calculator.trip_emissions_kg(20.0, 20.0, factors.HGV)
    e3 = calculator.trip_emissions_kg(30.0, 20.0, factors.HGV)
    assert e1 > 0
    assert e2 == pytest.approx(2.0 * e1, rel=1e-9)
    assert e3 == pytest.approx(3.0 * e1, rel=1e-9)


def test_trip_emissions_scale_linearly_with_payload():
    e1 = calculator.trip_emissions_kg(40.0, 5.0, factors.HGV)
    e2 = calculator.trip_emissions_kg(40.0, 10.0, factors.HGV)
    assert e2 == pytest.approx(2.0 * e1, rel=1e-9)


def test_trip_emissions_matches_published_factor():
    # 100 km * 10 t * 62 gCO2e/t-km = 62000 g = 62.0 kg.
    assert calculator.trip_emissions_kg(100.0, 10.0, factors.HGV) == pytest.approx(62.0)


# --- (b) idle emissions scale with minutes ----------------------------------
def test_idle_emissions_scale_linearly_with_minutes():
    i1 = calculator.idle_emissions_kg(30.0, factors.HGV)
    i2 = calculator.idle_emissions_kg(60.0, factors.HGV)
    i3 = calculator.idle_emissions_kg(90.0, factors.HGV)
    assert i1 > 0
    assert i2 == pytest.approx(2.0 * i1, rel=1e-9)
    assert i3 == pytest.approx(3.0 * i1, rel=1e-9)


def test_zero_idle_is_zero():
    assert calculator.idle_emissions_kg(0.0, factors.HGV) == 0.0


# --- (c) reefer class emits more than HGV for the same trip -----------------
def test_reefer_exceeds_hgv_for_same_trip():
    hgv = calculator.vehicle_emissions_kg(40.0, 20.0, 60.0, factors.HGV)
    reefer = calculator.vehicle_emissions_kg(40.0, 20.0, 60.0, factors.REEFER)
    assert reefer > hgv
    # Reefer is higher in both the moving leg and the idle leg.
    assert calculator.trip_emissions_kg(40.0, 20.0, factors.REEFER) > \
        calculator.trip_emissions_kg(40.0, 20.0, factors.HGV)
    assert calculator.idle_emissions_kg(60.0, factors.REEFER) > \
        calculator.idle_emissions_kg(60.0, factors.HGV)


# --- (d) aoi_rollup totals equal the sum of the parts -----------------------
def test_aoi_rollup_conserves_total():
    fleet = calculator.seed_aoi_fleet(200)
    r = calculator.aoi_rollup(fleet)

    assert r["vehicle_count"] == 200
    # by_source.moving + by_source.idle == total_kg (within rounding).
    parts = r["by_source"]["moving"] + r["by_source"]["idle"]
    assert parts == pytest.approx(r["total_kg"], abs=0.01)
    # by_class also sums to the total (within rounding).
    assert sum(r["by_class"].values()) == pytest.approx(r["total_kg"], abs=0.01)
    assert r["total_kg"] > 0


def test_aoi_rollup_is_deterministic():
    a = calculator.aoi_rollup(calculator.seed_aoi_fleet(200))
    b = calculator.aoi_rollup(calculator.seed_aoi_fleet(200))
    assert a == b


def test_aoi_rollup_empty_fleet():
    r = calculator.aoi_rollup([])
    assert r["total_kg"] == 0.0
    assert r["vehicle_count"] == 0
    assert r["by_source"] == {"moving": 0.0, "idle": 0.0}


def test_vehicle_emissions_is_moving_plus_idle():
    moving = calculator.trip_emissions_kg(25.0, 12.0, factors.RIGID)
    idle = calculator.idle_emissions_kg(45.0, factors.RIGID)
    total = calculator.vehicle_emissions_kg(25.0, 12.0, 45.0, factors.RIGID)
    assert total == pytest.approx(moving + idle, abs=0.01)
