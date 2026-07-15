"""Deployment-blocker regression: every ACTIVE driver must resolve to a fleet
vehicle. Reproduces the exact orphan scenario from the audit and proves the
backfill drives the verification query to zero rows WITHOUT mutating drivers."""
import os

import pytest

os.environ.setdefault("POSTGRES_DSN", "")
os.environ.setdefault("ALLOW_MEMORY_STORE", "true")

from gateway import enrollment as enr  # noqa: E402
from gateway import fleet  # noqa: E402

DSN = ""  # in-memory backend


@pytest.fixture(autouse=True)
def _clean():
    enr._MEM_DRIVERS.clear()
    enr._MEM.clear()
    enr._BACKEND.clear()
    fleet._MEM.clear()
    fleet._BACKEND.clear()
    yield
    enr._MEM_DRIVERS.clear()
    enr._MEM.clear()
    enr._BACKEND.clear()
    fleet._MEM.clear()
    fleet._BACKEND.clear()


async def _promote(driver_id: str, vehicle_no: str):
    await enr.promote_to_driver(
        DSN, {"driver_id": driver_id, "name": driver_id, "vehicle_no": vehicle_no},
        actor="test", photo_url=None, reference_image=None, template_dim=None,
        provider="admin")


# The exact three orphans from the audit: a plate assignment, a non-sim TRK id,
# and a driver whose id is TRK-shaped but whose vehicle is a plate.
ORPHANS = [("DV101", "MH04AB1234"), ("DRV-26E9A833", "TRK-000002"), ("TRK-000003", "MH04KN3106")]


@pytest.mark.asyncio
async def test_backfill_resolves_all_orphans_without_touching_drivers():
    for did, veh in ORPHANS:
        await _promote(did, veh)

    # Pre-condition: fleet is empty -> every ACTIVE driver is orphaned.
    before = await fleet.orphan_active_drivers(DSN)
    assert {o["driver_id"] for o in before} == {d for d, _ in ORPHANS}

    # Snapshot the assignments so we can prove they are NOT mutated.
    norms_before = {
        d: enr._MEM_DRIVERS[d]["vehicle_no_norm"] for d, _ in ORPHANS
    }

    inserted = await fleet.sync_from_assignments(DSN)
    assert inserted == len(ORPHANS)

    # Post-condition: verification query returns ZERO rows.
    assert await fleet.orphan_active_drivers(DSN) == []

    # Every assigned Vehicle ID now exists as a fleet vehicle_id.
    for _, veh in ORPHANS:
        vid = enr.normalize_vehicle_no(veh)
        assert await fleet.vehicle_exists(DSN, vid)

    # Drivers were NOT changed — assignments (and hence PWA login) are intact.
    for d, _ in ORPHANS:
        assert enr._MEM_DRIVERS[d]["vehicle_no_norm"] == norms_before[d]


@pytest.mark.asyncio
async def test_backfill_is_idempotent_and_preserves_existing_fleet_rows():
    # A curated fleet row must not be clobbered by the backfill.
    await fleet.add_vehicle(DSN, vehicle_id="TRK-000002", vehicle_number="OPS-EDIT",
                            vehicle_type="Reefer", created_by="admin:ops")
    await _promote("DRV-26E9A833", "TRK-000002")
    await _promote("DV101", "MH04AB1234")

    first = await fleet.sync_from_assignments(DSN)
    second = await fleet.sync_from_assignments(DSN)  # idempotent
    assert first == 1  # only MH04AB1234 was missing
    assert second == 0
    assert await fleet.orphan_active_drivers(DSN) == []
    # The pre-existing curated row survived untouched.
    row = await fleet.get_vehicle(DSN, "TRK-000002")
    assert row["vehicle_number"] == "OPS-EDIT" and row["vehicle_type"] == "Reefer"
