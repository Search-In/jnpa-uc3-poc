"""Carbon-emission persistence tests (UC-3 audit R6).

Covers the previously-missing durable ledger: the pure calculator record, the
POST /api/carbon/calculate compute-and-persist endpoint, and the GET
/api/carbon/history read-back. No live DB here (the DSN is an unreachable stub),
so the endpoint's figure is still returned while persistence best-efforts to
``emission_id: null`` / ``persisted: false`` — the calculation is never faked.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402


@pytest.fixture()
def client():
    from gateway.main import app

    with TestClient(app) as c:
        yield c


def test_emission_record_is_pure_and_grounded():
    from carbon import calculator

    rec = calculator.emission_record(
        vehicle_id="V1", distance_km=40, idle_minutes=30, vehicle_type="HGV"
    )
    # moving = 40 km * 20 t (nominal) * 62 gCO2e/t-km /1000; idle = 30 * 134 /1000.
    expected = round(40 * 20 * 62 / 1000, 3) + round(30 * 134 / 1000, 3)
    assert abs(rec["co2_kg"] - expected) < 0.01
    # Fuel is back-derived from CO2e via the published diesel factor (2680 gCO2e/L).
    assert rec["fuel_consumed_litre"] == round(rec["co2_kg"] * 1000 / 2680.0, 3)
    assert rec["vehicle_type"] == "HGV"
    assert "calculation_method" in rec


def test_calculate_endpoint_returns_figure(client):
    r = client.post(
        "/api/carbon/calculate",
        json={"vehicle_id": "MH04AB1234", "distance_km": 40,
              "idle_time_minutes": 30, "vehicle_type": "HGV"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["vehicle_id"] == "MH04AB1234"
    assert b["co2_kg"] > 0 and b["fuel_consumed_litre"] > 0
    assert b["source"] == "manual"
    assert "calculation_method" in b
    # No reachable DB in tests -> honest not-persisted result, figure still returned.
    assert b["persisted"] is False
    assert b["emission_id"] is None


def test_calculate_persists_via_committing_helper(client, monkeypatch):
    """Regression for the persistence bug: calculate() must write the INSERT with
    the COMMITTING execute_returning() (engine.begin), never fetch_one() (which runs
    on a non-committing engine.connect() and silently rolls the row back)."""
    import jnpa_shared.db as db
    from gateway.routers import carbon

    seen = {}

    async def fake_ensure(dsn):
        seen["ensure"] = True

    async def fake_execute_returning(sql, params=None, *, dsn=None):
        seen["sql"] = sql
        seen["co2"] = params["co2_kg"]
        assert "INSERT INTO jnpa.carbon_emission" in sql
        assert "RETURNING id" in sql
        return {"id": 4242}  # a committed INSERT ... RETURNING yields the new id

    async def forbidden_fetch_one(*a, **k):  # must NOT be used for the write
        raise AssertionError("calculate() must not persist via fetch_one()")

    monkeypatch.setattr(carbon, "_ensure", fake_ensure)
    monkeypatch.setattr(db, "execute_returning", fake_execute_returning)
    monkeypatch.setattr(db, "fetch_one", forbidden_fetch_one)

    r = client.post(
        "/api/carbon/calculate",
        json={"vehicle_id": "TRK-000001", "distance_km": 25,
              "idle_time_minutes": 20, "vehicle_type": "truck"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["emission_id"] == 4242
    assert b["persisted"] is True
    # The task's worked example: distance 25 * 20 t nominal * 62 /1000 + 20 * 134 /1000.
    assert abs(b["co2_kg"] - 33.68) < 0.01
    assert seen.get("ensure") is True


def test_calculate_requires_vehicle_id(client):
    r = client.post("/api/carbon/calculate", json={"distance_km": 10})
    assert r.status_code == 422


def test_history_endpoints_are_empty_without_db(client):
    r1 = client.get("/api/carbon/history/MH04AB1234")
    assert r1.status_code == 200
    assert r1.json()["records"] == [] and r1.json()["count"] == 0

    r2 = client.get("/api/carbon/history")
    assert r2.status_code == 200
    assert r2.json()["records"] == []
