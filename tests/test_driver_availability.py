"""Assign-Job driver availability — a driver holding an OPEN container job may
not be offered for a new one.

The exact counterpart of tests/test_vehicle_availability.py, and written against
the same two layers:

  * the SQL predicate ``gateway.enrollment`` sends to Postgres, asserted to be
    built from the job module's own status vocabulary (so a new status cannot
    silently make a busy driver look free), and
  * the availability rules themselves, exercised end-to-end against the in-memory
    backend: available / occupied / completed / cancelled / suspended / search /
    count.

Before this, ``GET /api/identity/drivers`` — the enrolled roster — was what the
Assign-Job dropdown read, and it had no job join at all: every ACTIVE driver was
offered, however many jobs they were already out on. The double-assignment guard
itself lives in tests/test_container_job.py (``driver_already_assigned`` + the
uq_job_open_driver partial unique index, migration 0140); here we only assert
that the dropdown and that guard agree.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("POSTGRES_DSN", "")
os.environ.setdefault("ALLOW_MEMORY_STORE", "true")

from gateway import enrollment as enr  # noqa: E402
from services.container_job.service import OPEN_STATES, STATUSES, TERMINAL  # noqa: E402

DSN = ""  # in-memory backend


@pytest.fixture(autouse=True)
def _clean():
    enr._MEM_DRIVERS.clear()
    enr._BACKEND.clear()
    yield
    enr._MEM_DRIVERS.clear()
    enr._BACKEND.clear()


def _register(driver_id: str, name: str, *, status: str = "ACTIVE",
              licence: str | None = None) -> str:
    """Seed a master driver directly, as approval does (enrollment.promote)."""
    enr._MEM_DRIVERS[driver_id] = {
        "driver_id": driver_id, "name": name, "license_no": licence,
        "vehicle_no": None, "vehicle_no_norm": None, "status": status,
        "photo_url": None,
    }
    return driver_id


# ------------------------------------------------------------------ SQL shape
def test_predicate_excludes_exactly_the_open_statuses():
    where, params = enr._assignable_drivers_where(None)
    assert "core.container_job_assignment" in where
    assert "NOT EXISTS" in where
    # The bound statuses are the TERMINAL ones (the ones that DON'T occupy a
    # driver); everything else in STATUSES is treated as occupied.
    bound = {v for k, v in params.items() if k.startswith("term")}
    assert bound == set(TERMINAL) == {"COMPLETED", "CANCELLED"}
    assert set(STATUSES) - bound == set(OPEN_STATES)
    assert "ASSIGNED" not in bound


def test_predicate_correlates_on_driver_id_not_on_a_null_check():
    """The rule is the job's STATE. Filtering ``driver_id IS NULL`` would leave a
    completed job occupying its driver for ever."""
    where, _ = enr._assignable_drivers_where(None)
    assert "j.driver_id = core.driver_identity.driver_id" in where
    assert "driver_id IS NULL" not in where


def test_assignable_where_requires_an_active_driver():
    where, params = enr._assignable_drivers_where(None)
    assert "status = :st" in where and params["st"] == enr.ACTIVE


def test_search_narrows_without_dropping_the_job_join():
    """The search and the availability rule share one WHERE, so a search can
    never reach past the exclusion."""
    where, params = enr._assignable_drivers_where("Aakash")
    assert "NOT EXISTS" in where and params["needle"] == "%AAKASH%"


# ------------------------------------------------------------------- rules
@pytest.mark.asyncio
async def test_free_driver_is_offered_and_counted():
    did = _register("DRV-1", "Rahul")
    rows = await enr.list_assignable_drivers(DSN, occupied=set())
    assert [r["driver_id"] for r in rows] == [did]
    assert await enr.count_assignable_drivers(DSN, occupied=set()) == 1


@pytest.mark.asyncio
async def test_driver_with_an_open_job_is_not_offered_nor_counted():
    """The ticket's example: Aakash is out on a job; Rahul, Suresh and Vijay are
    not. The dropdown returns the three, never Aakash."""
    busy = _register("DRV-A", "Aakash")
    _register("DRV-R", "Rahul")
    _register("DRV-S", "Suresh")
    _register("DRV-V", "Vijay")
    rows = await enr.list_assignable_drivers(DSN, occupied={busy})
    assert [r["name"] for r in rows] == ["Rahul", "Suresh", "Vijay"]
    assert await enr.count_assignable_drivers(DSN, occupied={busy}) == 3


@pytest.mark.asyncio
async def test_searching_an_occupied_driver_by_name_returns_nothing():
    """Acceptance 2: even asked for by name, an occupied driver is not returned —
    the search runs INSIDE the availability rule, not after it."""
    busy = _register("DRV-A", "Aakash")
    _register("DRV-R", "Rahul")
    assert await enr.list_assignable_drivers(DSN, q="Aakash", occupied={busy}) == []
    assert await enr.count_assignable_drivers(DSN, q="Aakash", occupied={busy}) == 0
    # …and the same search finds them once they are free again
    found = await enr.list_assignable_drivers(DSN, q="Aakash", occupied=set())
    assert [r["name"] for r in found] == ["Aakash"]


@pytest.mark.asyncio
async def test_search_matches_id_and_licence_too():
    _register("DRV-R", "Rahul", licence="UP6420140008203")
    assert len(await enr.list_assignable_drivers(DSN, q="drv-r", occupied=set())) == 1
    assert len(await enr.list_assignable_drivers(DSN, q="UP64201", occupied=set())) == 1
    assert await enr.list_assignable_drivers(DSN, q="nobody", occupied=set()) == []


@pytest.mark.asyncio
async def test_completed_or_cancelled_job_returns_the_driver_to_the_pool():
    """A terminal job is history: it must not hold a driver out of the pool. The
    caller's `occupied` set is built from OPEN statuses only, so a driver whose
    only jobs are COMPLETED/CANCELLED is simply absent from it."""
    did = _register("DRV-A", "Aakash")
    assert await enr.list_assignable_drivers(DSN, occupied={did}) == []
    # job COMPLETED / CANCELLED -> no longer in the occupied set
    assert [r["driver_id"] for r in await enr.list_assignable_drivers(DSN, occupied=set())] == [did]
    assert await enr.count_assignable_drivers(DSN, occupied=set()) == 1


@pytest.mark.asyncio
async def test_suspended_driver_is_never_assignable():
    """SUSPENDED is the only non-ACTIVE state core.driver_identity allows."""
    _register("DRV-X", "Suspended Person", status="SUSPENDED")
    assert await enr.list_assignable_drivers(DSN, occupied=set()) == []
    assert await enr.count_assignable_drivers(DSN, occupied=set()) == 0


@pytest.mark.asyncio
async def test_count_is_not_capped_by_the_page_limit():
    """The label shows how many are free, not how many fit on the page — 20 in
    the master with 1 busy must read "19 available", never "5"."""
    ids = [_register(f"DRV-{n:03d}", f"Driver {n:03d}") for n in range(20)]
    occupied = {ids[0]}
    page = await enr.list_assignable_drivers(DSN, limit=5, occupied=occupied)
    assert len(page) == 5
    assert await enr.count_assignable_drivers(DSN, occupied=occupied) == 19
    assert not {r["driver_id"] for r in page} & occupied


# --------------------------------------- duplicate records for ONE person
# core.driver_identity is keyed on driver_id alone: nothing stops the same
# licence appearing under several Driver IDs (the driver master import and the
# admin/PWA enrolment paths both create rows). That is what puts three identical
# "AAKIL KHAN — MH01 20100095262" options in the dropdown — and, far worse, keeps
# offering a driver who is out on a job under one of their OTHER records.
LICENCE = "MH01 20100095262"


def _same_person(*driver_ids: str) -> None:
    for did in driver_ids:
        _register(did, "AAKIL KHAN", licence=LICENCE)


@pytest.mark.asyncio
async def test_duplicate_records_for_one_person_yield_one_option():
    _same_person("DRV-7", "DRV-8", "DRV-9")
    _register("DRV-2", "Rahul", licence="UP6420140008203")
    rows = await enr.list_assignable_drivers(DSN, occupied=set())
    assert [r["driver_id"] for r in rows] == ["DRV-7", "DRV-2"]      # one AAKIL, not three
    assert await enr.count_assignable_drivers(DSN, occupied=set()) == 2


@pytest.mark.asyncio
async def test_a_busy_driver_is_not_offered_through_a_duplicate_record():
    """The bug behind "the dropdown shows drivers who already have an open job":
    the job is held by DRV-7, so the person is occupied — DRV-8 and DRV-9 are the
    same licence and must not stand in for them."""
    _same_person("DRV-7", "DRV-8", "DRV-9")
    _register("DRV-2", "Rahul", licence="UP6420140008203")
    # the caller's busy set names the licence the open job carries
    busy = {"DRV-7", "".join(c for c in LICENCE if c.isalnum())}
    rows = await enr.list_assignable_drivers(DSN, occupied=busy)
    assert [r["name"] for r in rows] == ["Rahul"]
    assert await enr.count_assignable_drivers(DSN, occupied=busy) == 1
    # …and searching for them by name finds nothing
    assert await enr.list_assignable_drivers(DSN, q="AAKIL", occupied=busy) == []


def test_the_dedupe_key_is_the_licence_not_the_display_name():
    """Two different people who happen to share a name stay two options."""
    assert "license_no" in enr._DRIVER_IDENTITY
    assert "name" not in enr._DRIVER_DEDUPE_KEY
    assert enr._normalise_licence("MH01 20100095262") == enr._normalise_licence("mh0120100095262")
    assert enr._normalise_licence("") == ""


def test_the_predicate_matches_the_licence_as_well_as_the_driver_id():
    where, _ = enr._assignable_drivers_where(None)
    assert "j.driver_id = core.driver_identity.driver_id" in where
    assert "driver_licence" in where          # the person, not just the record
