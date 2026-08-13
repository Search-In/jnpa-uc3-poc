"""Assign-Job vehicle availability — a vehicle holding an OPEN container job may
not be offered for a new one.

Two layers:
  * the SQL predicate ``gateway.fleet`` sends to Postgres, asserted to be built
    from the job module's own status vocabulary (so a new status cannot silently
    make a busy truck look free), and
  * the availability rules themselves, exercised end-to-end against the
    in-memory backend: available / occupied / completed / cancelled / count.

The double-assignment guard itself lives in tests/test_container_job.py
(``vehicle_already_assigned`` + the uq_job_open_vehicle partial unique index);
here we only assert that the dropdown and that guard agree.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_DSN", "")
os.environ.setdefault("ALLOW_MEMORY_STORE", "true")

from gateway import fleet  # noqa: E402
from services.container_job.service import OPEN_STATES, STATUSES, TERMINAL  # noqa: E402

DSN = ""  # in-memory backend


@pytest.fixture(autouse=True)
def _clean():
    fleet._MEM.clear()
    fleet._BACKEND.clear()
    yield
    fleet._MEM.clear()
    fleet._BACKEND.clear()


async def _register(vehicle_no: str, status: str = fleet.ACTIVE) -> str:
    vid = await fleet.next_vehicle_id(DSN)
    rec = await fleet.add_vehicle(DSN, vehicle_id=vid, vehicle_number=vehicle_no,
                                  status=status, created_by="test")
    return rec["vehicle_id"]


# ------------------------------------------------------------------ SQL shape
def test_predicate_excludes_exactly_the_open_statuses():
    sql, params = fleet._open_job_predicate()
    assert "core.container_job_assignment" in sql
    assert "NOT EXISTS" in sql
    # The bound values are the TERMINAL statuses (the ones that DON'T occupy a
    # vehicle); everything else in STATUSES is treated as occupied.
    assert set(params.values()) == set(TERMINAL) == {"COMPLETED", "CANCELLED"}
    assert set(STATUSES) - set(params.values()) == set(OPEN_STATES)
    assert "ASSIGNED" not in params.values()


def test_assignable_where_requires_active_and_a_vehicle_id():
    where, params = fleet._assignable_where(None)
    assert "status = :st" in where and params["st"] == fleet.ACTIVE
    assert "vehicle_id IS NOT NULL" in where
    assert "NOT EXISTS" in where


def test_search_narrows_without_dropping_the_job_join():
    where, params = fleet._assignable_where("MH04")
    assert "NOT EXISTS" in where and params["needle"] == "%MH04%"


# ------------------------------------------------------------------- rules
@pytest.mark.asyncio
async def test_free_vehicle_is_offered_and_counted():
    vid = await _register("MH04AA1111")
    rows = await fleet.list_assignable(DSN, occupied=set())
    assert [r["vehicle_id"] for r in rows] == [vid]
    assert await fleet.count_assignable(DSN, occupied=set()) == 1


@pytest.mark.asyncio
async def test_vehicle_with_an_open_job_is_not_offered_nor_counted():
    busy = await _register("MH04DV3973")
    free = await _register("MH04AA2222")
    rows = await fleet.list_assignable(DSN, occupied={busy})
    assert [r["vehicle_id"] for r in rows] == [free]
    assert await fleet.count_assignable(DSN, occupied={busy}) == 1


@pytest.mark.asyncio
async def test_completed_or_cancelled_job_returns_the_vehicle_to_the_pool():
    """A terminal job is history: it must not hold a truck out of the pool. The
    caller's `occupied` set is built from OPEN statuses only, so a truck whose
    only jobs are COMPLETED/CANCELLED is simply absent from it."""
    vid = await _register("MH04DV3973")
    assert [r["vehicle_id"] for r in await fleet.list_assignable(DSN, occupied={vid})] == []
    # job COMPLETED / CANCELLED -> no longer in the occupied set
    assert [r["vehicle_id"] for r in await fleet.list_assignable(DSN, occupied=set())] == [vid]
    assert await fleet.count_assignable(DSN, occupied=set()) == 1


@pytest.mark.asyncio
async def test_inactive_vehicle_is_never_assignable():
    await _register("MH04AA3333", status="MAINTENANCE")
    assert await fleet.list_assignable(DSN, occupied=set()) == []
    assert await fleet.count_assignable(DSN, occupied=set()) == 0


@pytest.mark.asyncio
async def test_count_is_not_capped_by_the_page_limit():
    """Acceptance A: the label shows how many are free, not how many fit on the
    page — 28 in the master with 8 busy must read "20 available", never "28"."""
    ids = [await _register(f"MH04AB{n:04d}") for n in range(28)]
    occupied = set(ids[:8])
    page = await fleet.list_assignable(DSN, limit=5, occupied=occupied)
    assert len(page) == 5
    assert await fleet.count_assignable(DSN, occupied=occupied) == 20
    assert not {r["vehicle_id"] for r in page} & occupied


@pytest.mark.asyncio
async def test_searching_an_occupied_vehicle_by_plate_returns_nothing():
    """The ticket's example: MH04QA9911 is on a job; MH04AB1234 and MH04CD5678
    are not. Asking for the busy plate by name returns nothing — the search runs
    INSIDE the availability rule, not after it."""
    busy = await _register("MH04QA9911")
    await _register("MH04AB1234")
    await _register("MH04CD5678")

    free = await fleet.list_assignable(DSN, occupied={busy})
    assert sorted(r["vehicle_number"] for r in free) == ["MH04AB1234", "MH04CD5678"]

    assert await fleet.list_assignable(DSN, q="MH04QA9911", occupied={busy}) == []
    assert await fleet.count_assignable(DSN, q="MH04QA9911", occupied={busy}) == 0
    # …and the same search finds it once the job is terminal
    found = await fleet.list_assignable(DSN, q="MH04QA9911", occupied=set())
    assert [r["vehicle_number"] for r in found] == ["MH04QA9911"]


# ------------------------------------- duplicate records for ONE truck
# core.vehicle is unique on vehicle_id; nothing guarantees one row per
# registration on a database whose table predates the fleet module's schema. A
# second row for the same plate is what lets an occupied truck keep appearing.
@pytest.mark.asyncio
async def test_duplicate_records_for_one_truck_yield_one_option():
    await _register("MH04QA9911")
    # a second master row for the same physical truck, punctuated differently
    vid2 = await fleet.next_vehicle_id(DSN)
    await fleet.add_vehicle(DSN, vehicle_id=vid2, vehicle_number="MH04 QA 9911",
                            status=fleet.ACTIVE, created_by="test")
    rows = await fleet.list_assignable(DSN, occupied=set())
    assert len(rows) == 1                                   # one truck, one option
    assert await fleet.count_assignable(DSN, occupied=set()) == 1


@pytest.mark.asyncio
async def test_a_busy_truck_is_not_offered_through_a_duplicate_record():
    """The bug behind "MH04QA9911 is assigned but still in the dropdown": the job
    is held by the first record, so the truck is occupied — its twin row must not
    stand in for it."""
    busy = await _register("MH04QA9911")
    vid2 = await fleet.next_vehicle_id(DSN)
    await fleet.add_vehicle(DSN, vehicle_id=vid2, vehicle_number="MH04 QA 9911",
                            status=fleet.ACTIVE, created_by="test")
    await _register("MH04AB1234")
    # what ContainerJobRepository.vehicles_with_open_jobs() returns: the Vehicle
    # ID the job was raised against AND the registration it recorded
    occupied = {busy, "MH04QA9911"}
    rows = await fleet.list_assignable(DSN, occupied=occupied)
    assert [r["vehicle_number"] for r in rows] == ["MH04AB1234"]
    assert await fleet.count_assignable(DSN, occupied=occupied) == 1
    assert await fleet.list_assignable(DSN, q="MH04QA9911", occupied=occupied) == []


def test_the_predicate_matches_the_registration_as_well_as_the_vehicle_id():
    sql, _ = fleet._open_job_predicate()
    assert "j.vehicle_id = core.vehicle.vehicle_id" in sql
    assert "vehicle_no" in sql                # the truck, not just the record


@pytest.mark.asyncio
async def test_a_busy_truck_is_not_offered_when_only_its_RECORD_id_is_known():
    """The regression this file previously missed: the caller's busy set names the
    Vehicle ID and NOTHING else, because the job row it was built from carries no
    registration of its own (3 of the 13 rows in the live spine are like that on
    the driver side). The truck is still busy — its twin record must not stand in
    for it, and the plate must not be searchable back into the list."""
    busy = await _register("MH04QA9911")
    vid2 = await fleet.next_vehicle_id(DSN)
    await fleet.add_vehicle(DSN, vehicle_id=vid2, vehicle_number="MH04 QA 9911",
                            status=fleet.ACTIVE, created_by="test")
    await _register("MH04AB1234")

    occupied = {busy}                       # the RECORD, not the registration
    rows = await fleet.list_assignable(DSN, occupied=occupied)
    assert [r["vehicle_number"] for r in rows] == ["MH04AB1234"]
    assert await fleet.count_assignable(DSN, occupied=occupied) == 1
    assert await fleet.list_assignable(DSN, q="MH04QA9911", occupied=occupied) == []
    assert await fleet.list_assignable(DSN, q="MH04 QA 9911", occupied=occupied) == []
    assert await fleet.count_assignable(DSN, q="MH04QA9911", occupied=occupied) == 0


@pytest.mark.asyncio
async def test_count_equals_the_unique_vehicles_returned():
    """Acceptance 9: the "N available" label and the list are one answer. Every
    truck is duplicated and a third of them are busy, so a count taken before the
    de-duplication or before the exclusion cannot agree with the list."""
    plates = [f"MH04AB{n:04d}" for n in range(9)]
    ids = [await _register(p) for p in plates]
    for p in plates:                        # a twin record for every truck
        vid = await fleet.next_vehicle_id(DSN)
        await fleet.add_vehicle(DSN, vehicle_id=vid, vehicle_number=p.replace("MH04", "MH04 "),
                                status=fleet.ACTIVE, created_by="test")
    occupied = set(ids[:3])
    rows = await fleet.list_assignable(DSN, limit=500, occupied=occupied)
    identities = {fleet._plate_identity(r["vehicle_number"]) for r in rows}
    assert len(rows) == len(identities) == 6
    assert await fleet.count_assignable(DSN, occupied=occupied) == len(rows)


def test_the_predicate_resolves_the_job_id_through_the_master():
    """A job that names only a Vehicle ID still occupies the TRUCK: the id is
    resolved to a registration through core.vehicle, so the twin record of a busy
    truck is excluded even when the job row carries no plate."""
    sql, _ = fleet._open_job_predicate()
    assert "FROM core.vehicle mv" in sql
    assert "mv.vehicle_id = j.vehicle_id" in sql
    assert "mv.vehicle_no" in sql
