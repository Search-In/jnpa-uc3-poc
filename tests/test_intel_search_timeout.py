"""Vehicle & Driver Intelligence search must answer inside the console's budget.

Regression cover for the 2026-08-12 production incident: searching MH04DV3973 on
/intelligence returned "Unable to load live data — 408 ETIMEDOUT". The plate has
no telemetry rows, and `core.idx_truck_telemetry_plate_ts` was INVALID (an
interrupted CONCURRENTLY build), so the telemetry leg of /api/vahan/vehicle-360
scanned 423 M rows before it could answer "none" — past 90 s. The whole profile
failed even though the vehicle, driver and transporter rows had resolved in
under 40 ms.

0141_intel_lookup_indexes.sql repairs the index. These tests pin the OTHER half
of the fix — that a slow or broken optional lookup can never again take the
primary identity data down with it — by driving the aggregates against a fake
`jnpa_shared.db` whose per-query behaviour each test chooses.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import jnpa_shared.db as shared_db  # noqa: E402
from gateway import vehicle_intel as vi  # noqa: E402

DSN = "postgresql+asyncpg://test/test"
PLATE = "MH04DV3973"
DL = "MH0420110012345"

DRIVER_ROW = {"driver_id": "DRV-91", "name": "S. Patil", "license_no": DL,
              "mobile": "97******12", "vehicle_no": PLATE, "status": "ACTIVE",
              "photo_url": None, "aadhaar_masked": None, "emergency_contact": None,
              "provider": None, "enrolled_at": None, "updated_at": None}
VEHICLE_ROW = {"vehicle_id": "TRK-000991", "vehicle_no": PLATE,
               "vehicle_type": "Container Truck", "chassis_number": "CH991",
               "rfid_fastag_id": "FT991", "status": "ACTIVE", "created_by": "ops",
               "created_at": None, "updated_at": None}


class FakeDB:
    """Stands in for jnpa_shared.db. Routes on a substring of the SQL.

    `rows` maps a table fragment -> the rows that query returns. `slow` maps a
    fragment -> seconds that query sleeps before answering (the pathological
    telemetry scan). `fails` maps a fragment -> an exception to raise (a provider
    that is down). Anything unmatched returns no rows, which is what the real
    schema does for a plate with no history.
    """

    def __init__(self, rows=None, slow=None, fails=None):
        self.rows, self.slow, self.fails = rows or {}, slow or {}, fails or {}
        self.calls: list[str] = []

    def _match(self, sql, table):
        return {k: v for k, v in table.items() if k in sql}

    async def fetch_all(self, sql, params=None, *, dsn=None):
        self.calls.append(sql)
        for _, exc in self._match(sql, self.fails).items():
            raise exc
        for _, delay in self._match(sql, self.slow).items():
            await asyncio.sleep(delay)
        for _, rows in self._match(sql, self.rows).items():
            return rows
        return []

    async def fetch_one(self, sql, params=None, *, dsn=None):
        rows = await self.fetch_all(sql, params, dsn=dsn)
        return rows[0] if rows else None


@pytest.fixture
def db(monkeypatch):
    """Install a FakeDB and shrink the budgets so the suite stays fast.

    The ratio is what matters, not the absolute values: primary > leg, and total
    bounds their sum. Production runs 8 / 5 / 12 against a 15 s client.
    """
    fake = FakeDB()
    monkeypatch.setattr(shared_db, "fetch_all", fake.fetch_all)
    monkeypatch.setattr(shared_db, "fetch_one", fake.fetch_one)
    monkeypatch.setattr(vi, "_PRIMARY_TIMEOUT_S", 0.40)
    monkeypatch.setattr(vi, "_LEG_TIMEOUT_S", 0.25)
    monkeypatch.setattr(vi, "_TOTAL_BUDGET_S", 0.90)
    return fake


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ the reported bug
def test_vehicle_360_returns_the_profile_for_the_reported_plate(db):
    """MH04DV3973 — the exact search from the incident — must resolve."""
    db.rows = {"SELECT vehicle_id, vehicle_no, vehicle_type": [VEHICLE_ROW], "FROM core.driver_identity d": [DRIVER_ROW]}
    r = run(vi.vehicle_360(PLATE, dsn=DSN))
    assert r["found"] is True
    assert r["vehicle"]["number"] == PLATE
    assert r["driver"]["name"] == "S. Patil"


def test_driver_search_returns_details_for_the_reported_plate(db):
    """Driver search resolves on the licence number carried by that vehicle."""
    db.rows = {"core.driver_identity WHERE driver_id": [DRIVER_ROW]}
    r = run(vi.driver_intel(DL, dsn=DSN))
    assert r["driver"]["driver_id"] == "DRV-91"
    assert r["driver"]["license_no"] == DL
    assert r["vehicle_no"] == PLATE


def test_normal_driver_search_does_not_timeout(db):
    """A healthy database answers far inside the budget, with every section."""
    db.rows = {"core.driver_identity WHERE driver_id": [DRIVER_ROW],
               "core.driver_license_lookup_history": [{"status": "VALID", "source": "SARATHI",
                                                       "response_payload": {}, "created_at": None}],
               "core.verification_log": [{"decision": "VERIFIED", "score": 0.9, "ts": None}],
               "core.violation_case": [{"case_id": "VC-1", "status": "OPEN", "total_fine": 500}]}
    t0 = time.monotonic()
    r = run(vi.driver_intel(DL, dsn=DSN))
    assert time.monotonic() - t0 < vi._LEG_TIMEOUT_S
    assert r["driver"] and len(r["dl_history"]) == 1
    assert len(r["activity"]) == 1 and len(r["violations"]) == 1


# ------------------------------------- a slow optional leg must not block primary
def test_slow_telemetry_does_not_block_the_vehicle_profile(db):
    """The incident, reproduced: telemetry hangs, everything else must survive.

    Before the fix this returned nothing at all — the gather waited on the
    telemetry leg and the console aborted at 15 s.
    """
    db.rows = {"SELECT vehicle_id, vehicle_no, vehicle_type": [VEHICLE_ROW], "FROM core.driver_identity d": [DRIVER_ROW]}
    db.slow = {"core.truck_telemetry": 30.0}
    t0 = time.monotonic()
    r = run(vi.vehicle_360(PLATE, dsn=DSN))
    elapsed = time.monotonic() - t0
    assert elapsed < vi._TOTAL_BUDGET_S, f"took {elapsed:.2f}s — would hit the client abort"
    assert r["found"] is True
    assert r["vehicle"]["number"] == PLATE          # primary survived...
    assert r["driver"]["name"] == "S. Patil"
    assert r["intel"]["tracking"] == []             # ...the slow section degraded


def test_slow_optional_leg_does_not_block_driver_details(db):
    db.rows = {"core.driver_identity WHERE driver_id": [DRIVER_ROW]}
    db.slow = {"core.driver_license_lookup_history": 30.0}
    t0 = time.monotonic()
    r = run(vi.driver_intel(DL, dsn=DSN))
    assert time.monotonic() - t0 < vi._TOTAL_BUDGET_S
    assert r["driver"]["driver_id"] == "DRV-91"
    assert r["dl_history"] == []


def test_every_optional_leg_slow_still_answers_within_budget(db):
    """Worst case — the whole intelligence half hangs. The response is still a
    response, because a degraded profile beats the client-side 408."""
    db.rows = {"SELECT vehicle_id, vehicle_no, vehicle_type": [VEHICLE_ROW]}
    db.slow = {"core.": 30.0}
    t0 = time.monotonic()
    r = run(vi.vehicle_360(PLATE, dsn=DSN))
    elapsed = time.monotonic() - t0
    assert elapsed < vi._TOTAL_BUDGET_S, f"took {elapsed:.2f}s"
    assert r["plate"] == PLATE
    assert r["intel"]["tracking"] == [] and r["driver"] is None
    # The timeline always carries the synthetic CURRENT_STATUS row; nothing
    # that depends on a hung lookup made it in.
    assert [e["stage"] for e in r["timeline"]] == ["CURRENT_STATUS"]


# ------------------------------------------- an unavailable provider degrades
def test_unavailable_optional_provider_degrades_gracefully(db):
    """A lookup that RAISES yields its empty section, not a failed request."""
    db.rows = {"SELECT vehicle_id, vehicle_no, vehicle_type": [VEHICLE_ROW], "FROM core.driver_identity d": [DRIVER_ROW]}
    db.fails = {"core.truck_telemetry": RuntimeError("relation unavailable"),
                "core.gate_event": RuntimeError("connection reset")}
    r = run(vi.vehicle_360(PLATE, dsn=DSN))
    assert r["found"] is True
    assert r["vehicle"]["number"] == PLATE
    assert r["intel"]["tracking"] == []


def test_unavailable_provider_degrades_for_driver_search(db):
    db.rows = {"core.driver_identity WHERE driver_id": [DRIVER_ROW]}
    db.fails = {"core.verification_log": RuntimeError("provider down")}
    r = run(vi.driver_intel(DL, dsn=DSN))
    assert r["driver"]["driver_id"] == "DRV-91"
    assert r["activity"] == []


# --------------------------------------------------- existing behaviour intact
def test_vehicle_intel_still_returns_every_section(db):
    """The /vehicle-intel contract is unchanged by the deadlines."""
    db.rows = {"core.vehicle_rc": [{"plate": PLATE, "vehicle_class": "HGV"}],
               "core.truck_telemetry": [{"ts": None, "lat": 18.9, "lon": 72.9, "speed_kmh": 40}],
               "core.violation_case": [{"case_id": "VC-9", "status": "OPEN",
                                        "total_fine": 1000, "first_detected_at": None}]}
    r = run(vi.vehicle_intel(PLATE, dsn=DSN))
    assert set(r) == {"vehicle_number", "rc", "tracking", "violations",
                      "challans", "alerts", "verification_history"}
    assert r["vehicle_number"] == PLATE
    assert r["rc"]["vehicle_class"] == "HGV"
    assert len(r["tracking"]) == 1 and len(r["violations"]) == 1
    assert r["challans"] == [] and r["alerts"] == []


def test_driver_search_supports_both_driver_id_and_licence_filters(db):
    """Search still matches on driver_id OR license_no, and the downstream
    lookups are keyed off whichever one resolved."""
    db.rows = {"core.driver_identity WHERE driver_id": [DRIVER_ROW]}
    by_id = run(vi.driver_intel("DRV-91", dsn=DSN))
    by_dl = run(vi.driver_intel(DL, dsn=DSN))
    assert by_id["driver"]["driver_id"] == by_dl["driver"]["driver_id"] == "DRV-91"
    assert any("driver_id = :k OR license_no = :k" in c for c in db.calls)


def test_no_dsn_still_returns_the_shape_stable_envelope(db):
    """Unchanged: no database configured is not an error, it is an empty profile."""
    assert run(vi.vehicle_360(PLATE, dsn=None))["found"] is False
    assert run(vi.driver_intel(DL, dsn=None)) == {}
