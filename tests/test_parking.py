"""Tests for the parking-availability service (Appendix C #1).

These exercise the pure occupancy / snapshot / summary functions in
``parking.facilities`` directly — no infrastructure required — so the live
availability board the dashboard renders can never silently drift from the
deterministic model. A couple of in-process API checks via Starlette's
TestClient confirm the endpoints surface the same numbers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
PARKING_DIR = REPO_ROOT  # repo root on path so the package imports as `parking`
for p in (str(SHARED_DIR), str(PARKING_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from parking import facilities as fac  # noqa: E402

ALL_MINUTES = range(fac.MINUTES_PER_DAY)
FACILITY_IDS = [f.id for f in fac.FACILITIES]


# --- (a) occupancy never exceeds capacity and is >= 0 for all minutes -------

def test_occupancy_within_bounds_for_every_minute():
    for facility in fac.FACILITIES:
        for minute in ALL_MINUTES:
            occ = fac.occupancy(facility.id, minute)
            assert isinstance(occ, int)
            assert 0 <= occ <= facility.capacity, (
                f"{facility.id} @ {minute}: {occ} out of [0, {facility.capacity}]"
            )


def test_occupancy_unknown_facility_raises():
    with pytest.raises(KeyError):
        fac.occupancy("PK-DOES-NOT-EXIST", 600)


# --- (b) snapshot is deterministic for a fixed minute_of_day ----------------

def test_snapshot_is_deterministic():
    a = fac.snapshot(615)
    b = fac.snapshot(615)
    assert a == b
    # And the minute is honoured modulo the day length.
    assert fac.snapshot(615) == fac.snapshot(615 + fac.MINUTES_PER_DAY)


def test_occupancy_is_deterministic():
    for facility in fac.FACILITIES:
        assert fac.occupancy(facility.id, 200) == fac.occupancy(facility.id, 200)


# --- (c) available == capacity - occupied for every facility ----------------

def test_available_equals_capacity_minus_occupied():
    for minute in (0, 360, 600, 720, 1080, 1439):
        for row in fac.snapshot(minute):
            assert row["available"] == row["capacity"] - row["occupied"]
            assert 0 <= row["available"] <= row["capacity"]


def test_utilisation_pct_consistent_with_occupied():
    for row in fac.snapshot(540):
        expected = round(100.0 * row["occupied"] / row["capacity"], 1)
        assert row["utilisation_pct"] == expected


# --- (d) status thresholds correct (full when <5% free) ---------------------

def test_status_thresholds():
    for minute in (0, 300, 600, 900, 1200, 1439):
        for row in fac.snapshot(minute):
            free_fraction = row["available"] / row["capacity"]
            if free_fraction > 0.20:
                assert row["status"] == fac.STATUS_AVAILABLE
            elif free_fraction >= 0.05:
                assert row["status"] == fac.STATUS_FILLING
            else:
                assert row["status"] == fac.STATUS_FULL


def test_status_full_when_under_five_percent_free():
    # Drive the private classifier directly across the boundaries.
    assert fac._status(available=4, capacity=100) == fac.STATUS_FULL     # 4% free
    assert fac._status(available=5, capacity=100) == fac.STATUS_FILLING  # 5% free
    assert fac._status(available=20, capacity=100) == fac.STATUS_FILLING  # 20% free
    assert fac._status(available=21, capacity=100) == fac.STATUS_AVAILABLE  # 21% free
    assert fac._status(available=0, capacity=100) == fac.STATUS_FULL


# --- (e) summary totals equal the sum over facilities -----------------------

def test_summary_totals_equal_sum_over_facilities():
    for minute in (0, 480, 720, 1000, 1439):
        rows = fac.snapshot(minute)
        s = fac.summary(minute)
        assert s["facilities"] == len(rows)
        assert s["total_capacity"] == sum(r["capacity"] for r in rows)
        assert s["total_occupied"] == sum(r["occupied"] for r in rows)
        assert s["total_available"] == sum(r["available"] for r in rows)
        assert s["full_count"] == sum(
            1 for r in rows if r["status"] == fac.STATUS_FULL
        )
        # Internal consistency of the totals.
        assert s["total_occupied"] + s["total_available"] == s["total_capacity"]


# --- inventory / geo-fence sanity -------------------------------------------

def test_inventory_matches_facilities():
    inv = fac.inventory()
    assert len(inv) == len(fac.FACILITIES)
    assert [r["facility_id"] for r in inv] == FACILITY_IDS
    for r, f in zip(inv, fac.FACILITIES):
        assert r["capacity"] == f.capacity
        assert r["vehicle_types"] == list(f.vehicle_types)


def test_facilities_outside_no_park_zones():
    # The import-time guard already asserts this; re-check explicitly here.
    from jnpa_shared.corridor import NO_PARK_ZONES, point_in_polygon

    for f in fac.FACILITIES:
        for zone in NO_PARK_ZONES:
            assert not point_in_polygon(f.lat, f.lon, zone.polygon)


def test_facilities_inside_geofenced_port_area():
    # All facilities sit in the JNPA port box near the gates (~18.86..18.95 N,
    # ~72.95..73.01 E).
    for f in fac.FACILITIES:
        assert 18.85 <= f.lat <= 18.96
        assert 72.94 <= f.lon <= 73.02


# --- in-process API checks --------------------------------------------------

def test_api_endpoints_match_pure_functions():
    from starlette.testclient import TestClient

    from parking.app import app

    with TestClient(app) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["facilities"] == len(fac.FACILITIES)

        avail = client.get("/availability", params={"minute_of_day": 630})
        assert avail.status_code == 200
        assert avail.json()["facilities"] == fac.snapshot(630)

        summ = client.get("/summary", params={"minute_of_day": 630})
        assert summ.status_code == 200
        body = summ.json()
        for k, v in fac.summary(630).items():
            assert body[k] == v

        inv = client.get("/facilities")
        assert inv.status_code == 200
        assert inv.json()["facilities"] == fac.inventory()


def test_api_rejects_out_of_range_minute():
    from starlette.testclient import TestClient

    from parking.app import app

    with TestClient(app) as client:
        # FastAPI Query(lt=1440) returns 422 for an out-of-range value.
        assert client.get("/availability", params={"minute_of_day": 1440}).status_code == 422
        assert client.get("/availability", params={"minute_of_day": -1}).status_code == 422
