"""POST /api/driver/login — driver sign-in by VEHICLE NUMBER.

The driver signs in with the registration painted on the truck (MH04LZ1507); the
gateway keys every driver record on the internal Vehicle ID (TRK-000011). This
endpoint is the bridge (number -> id), runs BEFORE any token exists, and changes
nothing about the token model or the database.

No server, no DB: the handler is called directly with ``fleet`` / ``enrollment``
monkeypatched, exactly as the repo's other router-logic tests do. A final group
statically pins the DRIVER-FACING UI to vehicle numbers — the TRK id must never
be advertised to a driver again.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway.routers import driver as D  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
STATE = SimpleNamespace(cfg=SimpleNamespace(postgres_dsn="postgresql://unused"))

VEHICLE = {"vehicle_id": "TRK-000011", "vehicle_number": "MH04LZ1507",
           "vehicle_type": "TRAILER", "status": "ACTIVE"}


def _login(monkeypatch, *, by_number=None, by_id=None, holder=None, body="MH04LZ1507"):
    async def find_by_number(dsn, number):
        assert number == number.upper()
        return by_number

    async def get_vehicle(dsn, vid):
        return by_id

    async def get_active_driver_by_vehicle(dsn, vid):
        return holder

    monkeypatch.setattr(D.fleet, "find_by_number", find_by_number)
    monkeypatch.setattr(D.fleet, "get_vehicle", get_vehicle)
    monkeypatch.setattr(D.enrollment, "get_active_driver_by_vehicle",
                        get_active_driver_by_vehicle)
    return asyncio.run(D.driver_login(D.DriverLoginBody(vehicle_number=body), STATE))


# ------------------------------------------------------------------ happy path
def test_login_with_vehicle_number_resolves_the_internal_id(monkeypatch):
    res = _login(monkeypatch, by_number=VEHICLE,
                 holder={"driver_id": "DRV-9", "name": "Ramesh"})
    assert res["vehicle_id"] == "TRK-000011"
    assert res["vehicle_number"] == "MH04LZ1507"
    assert res["driver_assigned"] is True
    assert res["driver_name"] == "Ramesh"


def test_login_is_case_and_separator_tolerant(monkeypatch):
    """The driver types what's painted on the truck — 'mh 04 lz 1507' must reach
    the lookup uppercased; the number stored in the master is what's returned."""
    res = _login(monkeypatch, by_number=VEHICLE, body="mh04lz1507")
    assert res["vehicle_id"] == "TRK-000011"


def test_login_without_an_assigned_driver_still_resolves(monkeypatch):
    """Parity with the old TRK-id flow: pairing never required an assigned
    driver, so the resolve must not either (enrollment happens in-app)."""
    res = _login(monkeypatch, by_number=VEHICLE, holder=None)
    assert res["driver_assigned"] is False
    assert res["driver_name"] is None


def test_operations_trk_id_path_still_works(monkeypatch):
    """Ops reading the pairing id to a driver over the phone must not be locked
    out — a TRK-###### input resolves via get_vehicle, not the plate lookup."""
    res = _login(monkeypatch, by_id=VEHICLE, body="TRK-000011")
    assert res["vehicle_id"] == "TRK-000011"
    assert res["vehicle_number"] == "MH04LZ1507"


# ------------------------------------------------------------------ failures
def test_unknown_vehicle_number_is_a_clean_404(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _login(monkeypatch, by_number=None)
    assert e.value.status_code == 404
    assert "isn't registered" in e.value.detail


def test_inactive_vehicle_is_refused_with_403(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _login(monkeypatch, by_number={**VEHICLE, "status": "MAINTENANCE"})
    assert e.value.status_code == 403


def test_empty_input_is_a_400(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _login(monkeypatch, by_number=VEHICLE, body="   ")
    assert e.value.status_code == 400


# ------------------------------------------------------------------ auth surface
def test_login_is_public_but_the_rest_of_the_driver_surface_is_not():
    """The resolve must run before any token exists (the DRIVER JWT is device-
    bound), so it is public — and that exemption must not leak onto /profile."""
    from gateway.auth import _is_public
    assert _is_public("/api/driver/login")
    assert not _is_public("/api/driver/profile")
    assert not _is_public("/api/driver")


# ------------------------------------------------------------------ UI contract
class TestTrkIdIsNotAdvertisedToDrivers:
    """The internal id may be ACCEPTED (ops support) but never ADVERTISED: no
    driver-facing sign-in copy may show a TRK example again. Static source pins,
    like the repo's SQL-shape tests."""

    PAIRING = (REPO / "mobile-pwa/src/screens/Pairing.tsx").read_text(encoding="utf-8")

    def test_signin_screen_shows_no_trk_example(self):
        assert "TRK-000123" not in self.PAIRING
        assert "Vehicle Number" in self.PAIRING

    def test_signin_placeholder_asks_for_the_vehicle_number(self):
        assert "Enter assigned vehicle number" in self.PAIRING

    def test_locales_advertise_numbers_not_ids(self):
        for lang in ("en", "hi", "mr"):
            src = (REPO / f"mobile-pwa/src/i18n/locales/{lang}.json").read_text(
                encoding="utf-8")
            assert "TRK-000123" not in src, f"{lang}.json still shows a TRK example"

    def test_home_and_profile_render_the_registration(self):
        """The session carries BOTH (id for APIs, number for eyes); the driver-
        facing strips must render via the number-first helper."""
        home = (REPO / "mobile-pwa/src/screens/Home.tsx").read_text(encoding="utf-8")
        profile = (REPO / "mobile-pwa/src/screens/Profile.tsx").read_text(encoding="utf-8")
        assert "displayVehicle(session)" in home
        assert "displayVehicle(session)" in profile
