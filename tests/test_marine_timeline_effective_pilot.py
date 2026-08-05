"""The timeline endpoint must use the EFFECTIVE pilot state, not just imported pilotage.

REGRESSION GUARD. `/api/marine/calls` (list) resolves its lifecycle through
MarineProjection, which fetches the manual assignment; `/api/marine/calls/{id}/timeline`
built its lifecycle by calling the pure `project()` directly and passed only the call and
its events. Pilot state is `imported OR manual OR pending`, so omitting the third input
made the timeline report `Pilot = Pending` for a vessel the list showed as `Onboard`.

These tests pin the two paths to the same answer. No database: the repository and the
manual-assignment reader are both injected.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from services.marine.manual_pilot import (STATUS_ASSIGNED, STATUS_ONBOARD,
                                          ManualPilotAssignment)
from services.marine.service import VesselCallService
from services.marine.state_engine import EVENT_PILOT_BOARDED

_T = datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)

_CALL = {"call_id": 22, "vcn": "INNSA1NF0R2968", "via_no": "R2968", "imo_no": "9291987",
         "vessel_name": "UAFL", "voyage_no": None, "status": "Berth Allotted",
         "terminal_id": None, "berth_id": None, "eta": _T, "etb": None, "etd": None,
         "ata": None, "atd": None, "atc": None}


class _Repo:
    """Returns one call plus whatever events the test supplies."""

    def __init__(self, events=()):
        self._events = list(events)

    async def timeline(self, call_id: int, *, data_origin=None):
        # `data_origin` is the LIVE/DEMO narrowing the service forwards verbatim; these
        # tests pin the lifecycle merge, so the double accepts it and ignores it.
        return {**_CALL, "call_id": call_id, "events": self._events}


class _Milestones:
    """Stands in for PilotMilestoneService — no DB, and no synthesised milestones.

    The timeline merges the milestones core.pilotage records (first line, all fast, pilot
    away, berth cleared) alongside the ledger's own events. The real reader opens a
    connection, so these tests inject an empty one and stay about the LEDGER plus the
    manual assignment, which is what this module exists to cover.
    """

    async def by_call_id(self, call_id: int):
        return []


class _Manual:
    """Stands in for ManualPilotService; `assignment` is what the reader would find."""

    def __init__(self, assignment=None):
        self._a = assignment

    async def resolve_effective_pilot(self, call_id: int):
        return self._a


def _assignment(status=STATUS_ONBOARD, boarded_at=_T):
    return ManualPilotAssignment(id=1, call_id=22, pilot_code="JP 24", status=status,
                                 boarded_at=boarded_at)


def _svc(events=(), assignment=None):
    return VesselCallService(repository=_Repo(events), manual=_Manual(assignment),
                             milestones=_Milestones())


@pytest.mark.asyncio
async def test_timeline_reports_a_manual_pilot_instead_of_pending():
    """The reported defect: a manual pilot existed and the timeline said Pending."""
    tl = await _svc(assignment=_assignment()).timeline(22)
    assert tl["lifecycle"]["pilot_state"] == "Onboard"
    assert tl["lifecycle"]["pilot_source"] == "manual"


@pytest.mark.asyncio
async def test_timeline_reports_assigned_before_boarding():
    tl = await _svc(assignment=_assignment(STATUS_ASSIGNED, None)).timeline(22)
    assert tl["lifecycle"]["pilot_state"] == "Assigned"


@pytest.mark.asyncio
async def test_timeline_is_pending_when_there_is_no_pilot_at_all():
    tl = await _svc().timeline(22)
    assert tl["lifecycle"]["pilot_state"] == "Pending"
    assert tl["lifecycle"]["pilot_source"] is None


@pytest.mark.asyncio
async def test_imported_boarding_outranks_a_manual_assignment_on_the_timeline():
    """Precedence must hold on this path exactly as it does on the list path."""
    events = [{"call_id": 22, "event_type": EVENT_PILOT_BOARDED,
               "event_ts": _T, "berth_id": None}]
    tl = await _svc(events=events, assignment=_assignment()).timeline(22)
    assert tl["lifecycle"]["pilot_state"] == "Active"       # the engine's word
    assert tl["lifecycle"]["pilot_source"] == "imported"


@pytest.mark.asyncio
async def test_manual_boarding_engages_port_craft_on_the_timeline_too():
    tl = await _svc(assignment=_assignment()).timeline(22)
    assert tl["lifecycle"]["portcraft_state"] == "Busy"


@pytest.mark.asyncio
async def test_timeline_still_returns_none_for_an_unknown_call():
    class _None(_Repo):
        async def timeline(self, call_id, *, data_origin=None):
            return None

    svc = VesselCallService(repository=_None(), manual=_Manual(),
                            milestones=_Milestones())
    assert await svc.timeline(999) is None


@pytest.mark.asyncio
async def test_lifecycle_keeps_every_field_the_response_model_reads():
    """Shape guard — the timeline JSON contract must not shift."""
    lc = (await _svc(assignment=_assignment()).timeline(22))["lifecycle"]
    for f in ("status", "arrival_state", "berth_state", "pilot_state", "departure_state",
              "shipping_state", "portcraft_state", "is_in_port", "is_at_berth",
              "latest_event", "latest_event_time"):
        assert f in lc, f
