"""Manual pilot assignment — the operator fallback when no pilot document was imported.

A vessel can complete VESPRO -> CALINF -> CALINV -> BERMAN -> BERALT and appear correctly
in Vessel Calls while no pilot card or pilot memo exists for it. This module lets an
operator record a pilot for that call and have every UC-I consumer see it, WITHOUT the
record ever being mistaken for imported data.

PRECEDENCE, IN ONE SENTENCE
---------------------------
Imported pilotage always wins: a manual assignment is deactivated the moment a pilot
document lands for the same call, and :func:`live_by_call_ids` — the only reader the
projection uses — never returns a row for a call that has imported pilotage.

That rule is enforced in THREE independent places, deliberately:
  * ``_SUPERSEDE``      — deactivates on import (called by the import path)
  * ``_LIVE``           — excludes imported calls at read time, so a missed supersede
                          still cannot surface manual data over imported data
  * ``uq_..._live``     — a partial unique index; one live assignment per call, ever

Writes here NEVER touch core.pilotage, core.vessel_call or core.vessel_call_event. The
imported ledger is read-only to this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .state_engine import (EVENT_PILOT_BOARDED, EVENT_PILOT_DISEMBARKED,
                           EVENT_PILOT_REQUESTED)

log = get_logger("services.marine.manual_pilot")

STATUS_ASSIGNED = "Assigned"
STATUS_ONBOARD = "Onboard"
STATUS_RELEASED = "Released"

_COLS = ("id, call_id, vcn, via_no, imo_no, vessel_name, pilot_code, pilot_name, "
         "status, assigned_at, boarded_at, released_at, created_by, created_at, "
         "updated_at, active, superseded_at")

_INSERT = f"""
INSERT INTO core.manual_pilot_assignment
    (call_id, vcn, via_no, imo_no, vessel_name, pilot_code, pilot_name, created_by)
SELECT :call_id, :vcn, :via_no, :imo_no, :vessel_name, :pilot_code, :pilot_name, :created_by
 WHERE NOT EXISTS (SELECT 1 FROM core.pilotage p WHERE p.call_id = :call_id)
RETURNING {_COLS}
"""

# Advance-only: a status may not move backwards, and a released assignment is terminal.
_BOARD = f"""
UPDATE core.manual_pilot_assignment
   SET status = '{STATUS_ONBOARD}', boarded_at = COALESCE(boarded_at, now()),
       updated_at = now()
 WHERE id = :id AND active AND status = '{STATUS_ASSIGNED}'
RETURNING {_COLS}
"""

_RELEASE = f"""
UPDATE core.manual_pilot_assignment
   SET status = '{STATUS_RELEASED}', released_at = COALESCE(released_at, now()),
       updated_at = now()
 WHERE id = :id AND active AND status <> '{STATUS_RELEASED}'
RETURNING {_COLS}
"""

_GET = f"SELECT {_COLS} FROM core.manual_pilot_assignment WHERE id = :id"

# The projection's reader. The NOT EXISTS is the belt to the supersede braces: even if a
# supersede were missed, imported pilotage still shadows the manual row here.
_LIVE = f"""
SELECT {_COLS} FROM core.manual_pilot_assignment m
 WHERE m.active AND m.call_id = ANY(:call_ids)
   AND NOT EXISTS (SELECT 1 FROM core.pilotage p WHERE p.call_id = m.call_id)
"""

_LIST = f"""
SELECT {_COLS} FROM core.manual_pilot_assignment
 WHERE (CAST(:active AS boolean) IS NULL OR active = CAST(:active AS boolean))
 ORDER BY id DESC LIMIT :limit OFFSET :offset
"""
_COUNT = ("SELECT count(*) FROM core.manual_pilot_assignment "
          "WHERE (CAST(:active AS boolean) IS NULL OR active = CAST(:active AS boolean))")

#: Deactivate every live manual assignment whose call now has imported pilotage.
#: Idempotent and non-destructive — the row is retained with `active = false`.
_SUPERSEDE = """
UPDATE core.manual_pilot_assignment m
   SET active = false, superseded_at = now(), updated_at = now()
 WHERE m.active
   AND EXISTS (SELECT 1 FROM core.pilotage p WHERE p.call_id = m.call_id)
RETURNING m.id
"""


# The manual milestone entering the SHARED event ledger. Same statement, same conflict key
# and same one-row-per-rung guard as manual_craft._LEDGER_INSERT — a manual pilot milestone
# is stored exactly like an imported one, so the Timeline and the engine cannot tell them
# apart. That is the point: one ledger, one state_engine, one projection.
#
# The WHERE NOT EXISTS is the semantic rule (a rung belongs to the CALL, not to each
# assignment); ON CONFLICT remains the concurrency backstop.
_LEDGER_INSERT = (
    "INSERT INTO core.vessel_call_event (call_id, event_type, event_ts, berth_id) "
    "SELECT :call_id, :event_type, :event_ts, NULL "
    " WHERE NOT EXISTS (SELECT 1 FROM core.vessel_call_event e "
    "                    WHERE e.call_id = :call_id AND e.event_type = :event_type) "
    "ON CONFLICT (call_id, event_type, event_ts) DO NOTHING"
)

#: Every pilot rung this module writes, for the chain reset below.
_PILOT_EVENTS = [EVENT_PILOT_REQUESTED, EVENT_PILOT_BOARDED, EVENT_PILOT_DISEMBARKED]

# A fresh assignment on a call whose previous pilot job finished must clear that job's
# rungs, or the ledger would hold a completed movement alongside the new one and the engine
# would read the call as still Completed. Scoped to the three rungs THIS module writes, so
# an imported VESARR/VESDEP milestone is never touched.
_CLEAR_PILOT_CHAIN = ("DELETE FROM core.vessel_call_event "
                      "WHERE call_id = :call_id AND event_type = ANY(:event_types) "
                      "  AND NOT EXISTS (SELECT 1 FROM core.pilotage p "
                      "                   WHERE p.call_id = :call_id)")


@dataclass(frozen=True)
class ManualPilotAssignment:
    """One operator-entered pilot assignment."""
    id: int
    call_id: int
    pilot_code: str
    status: str
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    pilot_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    boarded_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    active: bool = True
    superseded_at: Optional[datetime] = None

    @staticmethod
    def from_row(r: Mapping[str, Any]) -> "ManualPilotAssignment":
        return ManualPilotAssignment(
            id=int(r["id"]), call_id=int(r["call_id"]),
            pilot_code=str(r["pilot_code"]), status=str(r["status"]),
            vcn=r.get("vcn"), via_no=r.get("via_no"), imo_no=r.get("imo_no"),
            vessel_name=r.get("vessel_name"), pilot_name=r.get("pilot_name"),
            assigned_at=r.get("assigned_at"), boarded_at=r.get("boarded_at"),
            released_at=r.get("released_at"), created_by=r.get("created_by"),
            created_at=r.get("created_at"), updated_at=r.get("updated_at"),
            active=bool(r.get("active", True)), superseded_at=r.get("superseded_at"))


class ManualPilotService:
    """CRUD for manual assignments. Reads core.pilotage; never writes to it."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def assign(self, *, call_id: int, pilot_code: str,
                     vcn: Optional[str] = None, via_no: Optional[str] = None,
                     imo_no: Optional[str] = None, vessel_name: Optional[str] = None,
                     pilot_name: Optional[str] = None,
                     created_by: Optional[str] = None) -> Optional[ManualPilotAssignment]:
        """Create an assignment, or None when the call already has IMPORTED pilotage.

        None is a precedence refusal, not an error: the caller turns it into 409 so the
        operator is told the vessel already has real pilot data rather than silently
        getting a record that would never be shown.
        """
        params = {"call_id": int(call_id), "pilot_code": pilot_code.strip(),
                  "vcn": vcn, "via_no": via_no, "imo_no": imo_no,
                  "vessel_name": vessel_name, "pilot_name": pilot_name,
                  "created_by": created_by}
        try:
            async with get_engine(self._dsn).begin() as conn:
                row = (await conn.execute(text(_INSERT), params)).mappings().first()
                if row is not None:
                    # ONE transaction: an assignment absent from the ledger would be
                    # invisible to the Timeline, and a ledger rung with no assignment
                    # would be unattributable. Either both land or neither does.
                    await conn.execute(text(_CLEAR_PILOT_CHAIN),
                                       {"call_id": int(call_id),
                                        "event_types": _PILOT_EVENTS})
                    await conn.execute(text(_LEDGER_INSERT),
                                       {"call_id": int(call_id),
                                        "event_type": EVENT_PILOT_REQUESTED,
                                        "event_ts": row["assigned_at"]})
        except IntegrityError:
            # uq_manual_pilot_assignment_live: this call already has a live assignment.
            # Same refusal as the imported-pilotage case — the caller renders both as 409,
            # because in both the record would be shadowed the moment it was written.
            return None
        return ManualPilotAssignment.from_row(row) if row else None

    async def board(self, assignment_id: int) -> Optional[ManualPilotAssignment]:
        return await self._advance(_BOARD, assignment_id, EVENT_PILOT_BOARDED,
                                   "boarded_at")

    async def release(self, assignment_id: int) -> Optional[ManualPilotAssignment]:
        return await self._advance(_RELEASE, assignment_id, EVENT_PILOT_DISEMBARKED,
                                   "released_at")

    async def _advance(self, sql: str, assignment_id: int, milestone: str,
                       stamp_col: str) -> Optional[ManualPilotAssignment]:
        """Advance the assignment AND record the milestone, in one transaction.

        The ledger row carries the assignment's own stamp, so the Timeline shows the time
        the operator recorded rather than the time the row was written.
        """
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(sql),
                                      {"id": int(assignment_id)})).mappings().first()
            if row is not None:
                await conn.execute(text(_LEDGER_INSERT),
                                   {"call_id": int(row["call_id"]),
                                    "event_type": milestone,
                                    "event_ts": row[stamp_col]})
        return ManualPilotAssignment.from_row(row) if row else None

    async def get(self, assignment_id: int) -> Optional[ManualPilotAssignment]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(_GET), {"id": int(assignment_id)})).mappings().first()
        return ManualPilotAssignment.from_row(row) if row else None

    async def list(self, *, active: Optional[bool] = None,
                   limit: int = 100, offset: int = 0) -> tuple[list[ManualPilotAssignment], int]:
        async with get_engine(self._dsn).connect() as conn:
            p = {"active": active, "limit": limit, "offset": offset}
            rows = (await conn.execute(text(_LIST), p)).mappings().all()
            total = (await conn.execute(text(_COUNT), {"active": active})).scalar() or 0
        return [ManualPilotAssignment.from_row(r) for r in rows], int(total)

    async def live_by_call_ids(self, call_ids: Sequence[int]) -> dict[int, ManualPilotAssignment]:
        """Live assignments for these calls, keyed by call_id. The projection's reader."""
        ids = sorted({int(i) for i in call_ids if i is not None})
        if not ids:
            return {}
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(_LIVE), {"call_ids": ids})).mappings().all()
        return {int(r["call_id"]): ManualPilotAssignment.from_row(r) for r in rows}

    async def resolve_effective_pilot(self, call_id: int) -> Optional[ManualPilotAssignment]:
        """The manual assignment that should drive ONE call's pilot state, or None.

        THE PRECEDENCE RULE, and where each half of it lives
        ---------------------------------------------------
            IF imported pilotage exists   -> imported wins  (this returns None)
            ELSE IF a live manual exists  -> that assignment
            ELSE                          -> None, i.e. Pending

        The first branch is enforced by `_LIVE`'s ``NOT EXISTS (… core.pilotage …)``, so a
        call that has imported pilotage can never yield a manual row here. The projection
        then applies the same rule a second time against the ENGINE's verdict
        (:func:`services.marine.projection._merge_manual`) — that is what makes an imported
        PILOT_BOARDED event outrank a manual assignment even before a pilot card exists.

        SINGLE IMPLEMENTATION. This delegates to :meth:`live_by_call_ids` rather than
        carrying its own SQL, so there is one reader and one precedence predicate. It
        exists because the single-call paths (the timeline) hold one id and should not
        hand-roll a batch call to get at it.
        """
        return (await self.live_by_call_ids([call_id])).get(int(call_id))

    async def supersede_imported(self) -> int:
        """Deactivate manual assignments whose call gained imported pilotage.

        Called after every marine import. Idempotent — a second call deactivates nothing
        because the rows are already inactive.
        """
        async with get_engine(self._dsn).begin() as conn:
            rows = (await conn.execute(text(_SUPERSEDE))).mappings().all()
        if rows:
            log.info("manual pilot assignments superseded by import: %d", len(rows))
        return len(rows)


__all__ = ["ManualPilotAssignment", "ManualPilotService",
           "STATUS_ASSIGNED", "STATUS_ONBOARD", "STATUS_RELEASED"]
