"""Manual craft assignment — committing tugs, launches and mooring craft to a movement.

The craft counterpart to :mod:`services.marine.manual_pilot`, and deliberately its
structural twin: same precedence flag, same live-read predicate, same advance-only
transitions.

IT IS NOT A SECOND LIFECYCLE ENGINE. A commitment writes a CRAFT_ASSIGNED row into the
SHARED event ledger (core.vessel_call_event) in the same transaction, and the last
stand-down writes CRAFT_RELEASED. From there the state is derived by state_engine like any
imported milestone — this module decides nothing about craft_state itself.

WHY MANUAL IS THE ONLY SOURCE HERE
----------------------------------
Details_of_Port_Crafts.pdf gives the port's FLEET (core.port_craft, 18 craft) but no
per-call roster: the corpus never says which tug served which movement. So unlike pilots —
where an imported memo exists and always wins — craft assignment has no imported
counterpart today. ``active`` and the supersede path are still modelled so that precedence
has a home if such a feed arrives, rather than needing a migration then.

Writes NEVER touch core.port_craft or core.vessel_call. The only ledger rows written are
the two craft milestones above, through the same statement and the same conflict key the
import path uses.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from .state_engine import (EVENT_CRAFT_ASSIGNED, EVENT_CRAFT_ASSISTING,
                           EVENT_CRAFT_DISPATCHED, EVENT_CRAFT_ON_SCENE,
                           EVENT_CRAFT_RELEASED)

log = get_logger("services.marine.manual_craft")

STATUS_ASSIGNED = "Assigned"
STATUS_DISPATCHED = "Dispatched"
STATUS_ON_SCENE = "On Scene"
STATUS_ASSISTING = "Assisting"
STATUS_RELEASED = "Released"

#: The dispatch ladder, lowest first. Transitions may only advance along it.
LADDER: tuple[str, ...] = (STATUS_ASSIGNED, STATUS_DISPATCHED, STATUS_ON_SCENE,
                           STATUS_ASSISTING, STATUS_RELEASED)

#: status -> (column stamped when it is entered, ledger milestone it writes).
#: One table drives both, so a new rung cannot get a column without an event or an event
#: without a column — the drift that would make the ledger disagree with the record.
_RUNG = {
    STATUS_DISPATCHED: ("dispatched_at", EVENT_CRAFT_DISPATCHED),
    STATUS_ON_SCENE: ("arrived_at", EVENT_CRAFT_ON_SCENE),
    STATUS_ASSISTING: ("assisting_at", EVENT_CRAFT_ASSISTING),
    STATUS_RELEASED: ("released_at", EVENT_CRAFT_RELEASED),
}

_COLS = ("id, call_id, vcn, via_no, vessel_name, craft_id, craft_name, craft_type, "
         "status, assigned_at, dispatched_at, arrived_at, assisting_at, released_at, "
         "created_by, created_at, updated_at, active, superseded_at")

_INSERT = (
    "INSERT INTO core.manual_craft_assignment "
    "(call_id, vcn, via_no, vessel_name, craft_id, craft_name, craft_type, created_by) "
    "VALUES (:call_id, :vcn, :via_no, :vessel_name, :craft_id, :craft_name, "
    ":craft_type, :created_by) "
    f"RETURNING {_COLS}"
)

_GET = f"SELECT {_COLS} FROM core.manual_craft_assignment WHERE id = :id"

#: The projection's reader — live commitments for a page of calls.
_LIVE = (f"SELECT {_COLS} FROM core.manual_craft_assignment "
         "WHERE active AND call_id = ANY(:call_ids)")

_LIST = (f"SELECT {_COLS} FROM core.manual_craft_assignment "
         "WHERE (CAST(:active AS boolean) IS NULL OR active = CAST(:active AS boolean)) "
         "ORDER BY id DESC LIMIT :limit OFFSET :offset")

_COUNT = ("SELECT count(*) FROM core.manual_craft_assignment "
          "WHERE (CAST(:active AS boolean) IS NULL OR active = CAST(:active AS boolean))")

# The craft commitment entering the SHARED event ledger. Same statement shape and the same
# ON CONFLICT key the import path uses (repository._EVENT_INSERT), so a manual milestone is
# stored exactly like an imported one and the engine cannot tell them apart — which is the
# point: one ledger, one state_engine, one projection.
#
# berth_id is null: a craft commitment happens to a MOVEMENT, not at a berth.
# ONE ROW PER RUNG PER CHAIN. The ON CONFLICT key is (call_id, event_type, event_ts), so
# it only stops a same-instant repeat — a second tug joining a live movement, or a second
# tug dispatching, carries a NEW timestamp and would add a duplicate milestone. The
# WHERE NOT EXISTS closes that: a craft milestone is a fact about the CALL's engagement
# ("craft were committed to this movement"), not about each individual craft, so the
# second tug does not re-open a stage the first already reached.
#
# Both guards are kept: the WHERE NOT EXISTS is the semantic rule, ON CONFLICT remains the
# concurrency backstop for two requests racing on the same instant.
_LEDGER_INSERT = (
    "INSERT INTO core.vessel_call_event (call_id, event_type, event_ts, berth_id) "
    "SELECT :call_id, :event_type, :event_ts, NULL "
    " WHERE NOT EXISTS (SELECT 1 FROM core.vessel_call_event e "
    "                    WHERE e.call_id = :call_id AND e.event_type = :event_type) "
    "ON CONFLICT (call_id, event_type, event_ts) DO NOTHING"
)

# A release event is only the truth for the CALL once the last craft has stood down —
# one tug finishing while three keep working has not ended the call's craft engagement.
_STILL_COMMITTED = (
    "SELECT count(*) FROM core.manual_craft_assignment "
    "WHERE call_id = :call_id AND active AND status <> 'Released' AND id <> :exclude"
)

# Re-committing after a full stand-down must clear the stale CRAFT_RELEASED, or the ledger
# would hold both and the engine would read the call as released while craft are out.
# Re-committing after a full stand-down must clear the PREVIOUS craft chain, or the ledger
# would hold a completed job's rungs alongside the new one and the engine would read the
# call as still Assisting from a movement that finished hours ago.
_CLEAR_CRAFT_CHAIN = ("DELETE FROM core.vessel_call_event "
                      "WHERE call_id = :call_id AND event_type = ANY(:event_types)")

#: Every craft milestone, for the chain reset above.
_CRAFT_EVENTS = [EVENT_CRAFT_ASSIGNED, EVENT_CRAFT_DISPATCHED, EVENT_CRAFT_ON_SCENE,
                 EVENT_CRAFT_ASSISTING, EVENT_CRAFT_RELEASED]


@dataclass(frozen=True)
class ManualCraftAssignment:
    """One craft committed to one movement."""
    id: int
    call_id: int
    craft_id: int
    status: str
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    craft_name: Optional[str] = None
    craft_type: Optional[str] = None
    assigned_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    assisting_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    active: bool = True
    superseded_at: Optional[datetime] = None

    @staticmethod
    def from_row(r: Mapping[str, Any]) -> "ManualCraftAssignment":
        return ManualCraftAssignment(
            id=int(r["id"]), call_id=int(r["call_id"]), craft_id=int(r["craft_id"]),
            status=str(r["status"]), vcn=r.get("vcn"), via_no=r.get("via_no"),
            vessel_name=r.get("vessel_name"), craft_name=r.get("craft_name"),
            craft_type=r.get("craft_type"), assigned_at=r.get("assigned_at"),
            dispatched_at=r.get("dispatched_at"), arrived_at=r.get("arrived_at"),
            assisting_at=r.get("assisting_at"), released_at=r.get("released_at"),
            created_by=r.get("created_by"), created_at=r.get("created_at"),
            updated_at=r.get("updated_at"), active=bool(r.get("active", True)),
            superseded_at=r.get("superseded_at"))


class ManualCraftService:
    """CRUD for craft commitments. Reads core.port_craft; never writes to it."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def assign(self, *, call_id: int, craft_id: int,
                     vcn: Optional[str] = None, via_no: Optional[str] = None,
                     vessel_name: Optional[str] = None, craft_name: Optional[str] = None,
                     craft_type: Optional[str] = None,
                     created_by: Optional[str] = None) -> Optional[ManualCraftAssignment]:
        """Commit a craft, or None when it is already out on another movement.

        None is a refusal, not an error: the caller renders it 409 so the operator is told
        the craft is engaged rather than silently double-booking it.
        """
        params = {"call_id": int(call_id), "craft_id": int(craft_id), "vcn": vcn,
                  "via_no": via_no, "vessel_name": vessel_name,
                  "craft_name": craft_name, "craft_type": craft_type,
                  "created_by": created_by}
        try:
            async with get_engine(self._dsn).begin() as conn:
                row = (await conn.execute(text(_INSERT), params)).mappings().first()
                if row is not None:
                    # ONE transaction: a commitment that is not in the ledger would be
                    # invisible to the engine, and a ledger event with no commitment would
                    # be unattributable. Either both land or neither does.
                    # Only when the previous chain FINISHED. A second craft joining a
                    # live movement must not erase the rungs the first one already made.
                    prior = (await conn.execute(
                        text(_STILL_COMMITTED),
                        {"call_id": int(call_id), "exclude": int(row["id"])})).scalar() or 0
                    if prior == 0:
                        await conn.execute(text(_CLEAR_CRAFT_CHAIN),
                                           {"call_id": int(call_id),
                                            "event_types": _CRAFT_EVENTS})
                    await conn.execute(text(_LEDGER_INSERT),
                                       {"call_id": int(call_id),
                                        "event_type": EVENT_CRAFT_ASSIGNED,
                                        "event_ts": row["assigned_at"]})
        except IntegrityError:
            return None  # uq_manual_craft_assignment_live: craft already committed
        return ManualCraftAssignment.from_row(row) if row else None

    async def advance(self, assignment_id: int,
                      to: str) -> Optional[ManualCraftAssignment]:
        """Move one commitment along the dispatch ladder.

        ADVANCE-ONLY, enforced in SQL against LADDER itself rather than by a chain of
        per-transition statements — so adding a rung is a change to LADDER, not new code.
        A status may never move backwards and Released is terminal.
        """
        rung = _RUNG.get(to)
        if rung is None:
            return None
        stamp_col, milestone = rung
        allowed = list(LADDER[:LADDER.index(to)])  # strictly-earlier states may advance
        if not allowed:
            return None
        sql = (
            "UPDATE core.manual_craft_assignment "
            f"SET status = :to, {stamp_col} = COALESCE({stamp_col}, now()), "
            "updated_at = now() "
            "WHERE id = :id AND active AND status = ANY(:allowed) "
            f"RETURNING {_COLS}"
        )
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(
                text(sql), {"id": int(assignment_id), "to": to,
                            "allowed": allowed})).mappings().first()
            if row is not None:
                # EVERY rung writes its milestone, in the same transaction as the record —
                # a transition the ledger never saw would be invisible to the engine and to
                # the Timeline, which is exactly the gap this closes.
                #
                # RELEASED is the one exception, and only in WHEN it is written: it is the
                # CALL's craft engagement ending, so it waits for the last craft to stand
                # down. One tug finishing while three keep working has ended nothing.
                emit = True
                if to == STATUS_RELEASED:
                    remaining = (await conn.execute(
                        text(_STILL_COMMITTED),
                        {"call_id": int(row["call_id"]),
                         "exclude": int(row["id"])})).scalar() or 0
                    emit = remaining == 0
                if emit:
                    await conn.execute(text(_LEDGER_INSERT),
                                       {"call_id": int(row["call_id"]),
                                        "event_type": milestone,
                                        "event_ts": row[stamp_col]})
        return ManualCraftAssignment.from_row(row) if row else None

    async def release(self, assignment_id: int) -> Optional[ManualCraftAssignment]:
        return await self.advance(assignment_id, STATUS_RELEASED)

    async def get(self, assignment_id: int) -> Optional[ManualCraftAssignment]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(_GET),
                                      {"id": int(assignment_id)})).mappings().first()
        return ManualCraftAssignment.from_row(row) if row else None

    async def list(self, *, active: Optional[bool] = None, limit: int = 200,
                   offset: int = 0) -> tuple[list[ManualCraftAssignment], int]:
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(
                text(_LIST), {"active": active, "limit": limit,
                              "offset": offset})).mappings().all()
            total = (await conn.execute(text(_COUNT), {"active": active})).scalar() or 0
        return [ManualCraftAssignment.from_row(r) for r in rows], int(total)

    async def live_by_call_ids(
            self, call_ids: Sequence[int]) -> dict[int, list[ManualCraftAssignment]]:
        """Live commitments per call.

        A LIST per call, unlike the pilot reader: a movement routinely takes several tugs
        plus a launch, whereas one call has at most one pilot.
        """
        ids = sorted({int(i) for i in call_ids if i is not None})
        if not ids:
            return {}
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(_LIVE),
                                       {"call_ids": ids})).mappings().all()
        out: dict[int, list[ManualCraftAssignment]] = {}
        for r in rows:
            out.setdefault(int(r["call_id"]), []).append(
                ManualCraftAssignment.from_row(r))
        return out


__all__ = ["ManualCraftAssignment", "ManualCraftService", "LADDER",
           "STATUS_ASSIGNED", "STATUS_DISPATCHED", "STATUS_ON_SCENE",
           "STATUS_ASSISTING", "STATUS_RELEASED"]
