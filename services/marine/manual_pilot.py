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

Writes here NEVER touch core.pilotage. The imported PILOTAGE ledger is read-only to this
module.

core.vessel_call IS written, by release and only by release: the `ata`/`atd` actual the
declared leg completed, and only where that column is still NULL (_STAMP_ACTUAL). Those
columns belong to the VESARR/VESDEP import path, which rewrites them unconditionally, so
the NULL guard is what keeps an operator record from ever overwriting a real actual while
still letting a manually run movement be visible to every consumer that reads the columns
rather than the projection.

core.vessel_call_event IS written — the pilot rungs, and on release the visit milestone the
declared leg implies (migration 0054). That is deliberate and is what makes a manual
movement indistinguishable from an imported one to the Timeline and the state engine: one
ledger, one derive_state, one projection. Every insert goes through _LEDGER_INSERT, whose
NOT EXISTS guard means an imported milestone is never duplicated or overwritten.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

if TYPE_CHECKING:  # `projection` imports THIS module, so the runtime import is deferred.
    from .projection import CallProjection

from .state_engine import (EVENT_BERTH_VACATED, EVENT_BERTHED, EVENT_PILOT_BOARDED,
                           EVENT_PILOT_DISEMBARKED, EVENT_PILOT_REQUESTED,
                           EVENT_SAILED)

log = get_logger("services.marine.manual_pilot")

STATUS_ASSIGNED = "Assigned"
STATUS_ONBOARD = "Onboard"
STATUS_RELEASED = "Released"

_COLS = ("id, call_id, vcn, via_no, imo_no, vessel_name, pilot_code, pilot_name, "
         "status, movement_type, berth_id, assigned_at, boarded_at, released_at, "
         "created_by, created_at, updated_at, active, superseded_at")

MOVEMENT_INWARD = "INWARD"
MOVEMENT_OUTWARD = "OUTWARD"
MOVEMENT_SHIFTING = "SHIFTING"

#: Accepted movement legs, mirroring core.pilotage.movement_type and the CHECK added by
#: migration 0054. Anything else is rejected at the edge rather than stored.
MOVEMENTS = (MOVEMENT_INWARD, MOVEMENT_OUTWARD, MOVEMENT_SHIFTING)

#: THE MILESTONE EACH LEG COMPLETES.
#:
#: A pilot disembarking is ambiguous on its own — inbound he steps off once the vessel is
#: fast alongside, outbound once she is clear — so the leg the operator declared is what
#: decides. SHIFTING ends alongside a different berth, so it berths her exactly as an
#: inbound movement does.
#:
#: OUTWARD records SAILED, not DEPARTED: the vessel has left her berth and is outbound,
#: which is what derive_state calls 'Sailing'. DEPARTED means cleared of port limits — a
#: later, separate fact that no pilot release can attest to.
#:
#: A leg absent from this map (None, i.e. a pre-0054 assignment) records no milestone, so
#: those rows behave exactly as they did before this feature.
_MOVEMENT_MILESTONE = {
    MOVEMENT_INWARD: EVENT_BERTHED,
    MOVEMENT_SHIFTING: EVENT_BERTHED,
    MOVEMENT_OUTWARD: EVENT_SAILED,
}

#: SECOND milestone, for a leg that completes two facts at once.
#:
#: Sailing frees the berth, and the engine will not infer it: berth_state reads
#: `departed or berth_vacated`, so a vessel that berthed and then SAILED stayed 'Occupied'
#: for ever — verified against derive_state directly. BERTH_VACATED is the berth's own
#: release signal and precedes DEPARTED in the ladder, which is exactly the fact an outward
#: pilot release can attest to: she is off the berth. DEPARTED (cleared port limits) is
#: still left to the VESDEP import.
#:
#: An inward or shifting movement adds nothing here — she is arriving at a berth, not
#: leaving one.
_MOVEMENT_EXTRA_MILESTONE = {
    MOVEMENT_OUTWARD: EVENT_BERTH_VACATED,
}

class IllegalMovement(ValueError):
    """The declared leg is impossible from where the vessel actually is.

    Distinct from the precedence refusal (which returns None -> 409): that says "someone
    else owns this call", this says "the vessel cannot do that from here". The caller
    renders it as 422 with `allowed`, so the client can correct rather than guess.
    """

    def __init__(self, movement: str, allowed: Sequence[str], reason: str) -> None:
        super().__init__(reason)
        self.movement = movement
        self.allowed = tuple(allowed)
        self.reason = reason


#: Why each leg is impossible, when it is. Keyed by leg; formatted into the 422 detail.
_ILLEGAL_REASON = {
    MOVEMENT_INWARD: "she has already arrived — there is nothing to bring in",
    MOVEMENT_SHIFTING: "she is not at a berth — there is nothing to shift from",
    MOVEMENT_OUTWARD: "she is not in port — she cannot depart before she arrives",
}


def legal_movements(state: "CallProjection") -> tuple[str, ...]:
    """Which legs are PHYSICALLY POSSIBLE from the vessel's current position. Pure.

    Derived from the projection, never from stored columns — the same source the pilot
    eligibility rules read. Each clause is a statement about where she is:

        INWARD    she has not arrived yet; that is what an inward movement is for
        SHIFTING  she is AT A BERTH — a shift is berth-to-berth
        OUTWARD   she is IN PORT — a vessel that never arrived cannot depart

    Measured against the live corpus, 1080 of the 1505 assignable calls had not arrived,
    so before this guard every one of them could be recorded as shifting from a berth it
    was not at, or sailing without ever having arrived.

    THE UI APPLIES THE SAME RULES (web pilotDesk.ts::legalMovements) so the picker can
    disable an impossible leg without a round trip. That copy is an affordance; THIS is
    the authority, and it is what a direct API call meets.
    """
    legal: list[str] = []
    if state.arrival_state != "Completed":
        legal.append(MOVEMENT_INWARD)
    if state.is_at_berth:
        legal.append(MOVEMENT_SHIFTING)
    if state.is_in_port:
        legal.append(MOVEMENT_OUTWARD)
    return tuple(legal)


#: Read a call and its milestones to derive where she is. Both statements run inside the
#: assign transaction, so the position cannot change between the check and the insert.
_CALL_FOR_STATE = "SELECT status FROM core.vessel_call WHERE call_id = :call_id"
_EVENTS_FOR_STATE = ("SELECT event_type, event_ts FROM core.vessel_call_event "
                     "WHERE call_id = :call_id")


#: Legs that MOVE the vessel to the declared berth. On release the destination is written
#: onto the BERTHED milestone and onto core.vessel_call.berth_id.
#:
#: OUTWARD is absent: she is leaving, and overwriting berth_id on the way out would erase
#: the record of where she sailed from.
_MOVEMENT_BERTHS = (MOVEMENT_INWARD, MOVEMENT_SHIFTING)

#: THE ACTUALS COLUMN EACH LEG STAMPS on core.vessel_call.
#:
#: The event ledger is not enough. derive_state reads core.vessel_call_event, so UC-I's own
#: screens moved correctly the moment the milestone landed — but `ata`/`atd` are COLUMNS,
#: and every consumer that reads them directly saw nothing. UC-2's Export -> Departures is
#: the case that exposed this: it fetches /api/marine/calls and filters `!!atd`, so a
#: manually sailed vessel was invisible to it however complete its lifecycle looked here.
#:
#: SHIFTING stamps NOTHING. A berth-to-berth shift is neither an arrival at the port nor a
#: departure from it — the vessel was already alongside and still is. It records BERTHED
#: above (she is fast at a new berth) and touches neither actual, because writing `ata` for
#: a vessel that arrived days ago would be a falsehood, not an approximation.
_MOVEMENT_ACTUAL = {
    MOVEMENT_INWARD: "ata",
    MOVEMENT_OUTWARD: "atd",
}

#: Stamp the actual for a completed manual movement — but ONLY over NULL.
#:
#: THE COALESCE IS THE WHOLE SAFETY ARGUMENT. `ata`/`atd` are IMPORTED actuals: VESARR and
#: VESDEP own them, and the import path rewrites them on re-import and Override Import. An
#: operator's record must never overwrite a real one, so this fires only where Customs and
#: the PCS have said nothing yet. The column name is interpolated from _MOVEMENT_ACTUAL —
#: never from caller input — so it cannot be anything but 'ata' or 'atd'.
#:
#: A later import then wins by construction: it writes the column unconditionally, and this
#: statement will not fire again because the value is no longer NULL.
#: Move the call to the berth the completed movement put her at.
#:
#: UNCONDITIONAL, unlike _STAMP_ACTUAL. `berth_id` is not an append-only actual: BERALT
#: sets it, and a SHIFTING movement is BY DEFINITION a change to it, so a NULL guard would
#: make every shift a no-op — the exact bug this fixes. A later import still wins the
#: ordinary way, by writing the column again.
_MOVE_BERTH = """
UPDATE core.vessel_call
   SET berth_id = :berth_id, updated_at = now()
 WHERE call_id = :call_id AND berth_id IS DISTINCT FROM :berth_id
"""

_STAMP_ACTUAL = """
UPDATE core.vessel_call
   SET {col} = :ts, updated_at = now()
 WHERE call_id = :call_id AND {col} IS NULL
"""

_INSERT = f"""
INSERT INTO core.manual_pilot_assignment
    (call_id, vcn, via_no, imo_no, vessel_name, pilot_code, pilot_name, created_by,
     movement_type, berth_id)
SELECT :call_id, :vcn, :via_no, :imo_no, :vessel_name, :pilot_code, :pilot_name,
       :created_by, :movement_type, :berth_id
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
    # berth_id was hardcoded NULL here, so a manual BERTHED could not say WHERE she was
    # fast — the imported path has always written it (BERALT's BERTH_ALLOTTED names the
    # berth). A parameter now; callers with no berth pass None and the statement behaves
    # exactly as before.
    "SELECT :call_id, :event_type, :event_ts, :berth_id "
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
    #: INWARD | OUTWARD | SHIFTING, or None for an assignment made before migration 0054.
    movement_type: Optional[str] = None
    #: Destination berth the operator declared. Required for SHIFTING, optional for
    #: INWARD, always None for OUTWARD. See migration 0055.
    berth_id: Optional[int] = None
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
            movement_type=r.get("movement_type"),
            berth_id=r.get("berth_id"),
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
                     created_by: Optional[str] = None,
                     movement_type: Optional[str] = None,
                     berth_id: Optional[int] = None) -> Optional[ManualPilotAssignment]:
        """Create an assignment, or None when the call already has IMPORTED pilotage.

        None is a precedence refusal, not an error: the caller turns it into 409 so the
        operator is told the vessel already has real pilot data rather than silently
        getting a record that would never be shown.
        """
        # Normalised here so the CHECK constraint is never the thing that reports a
        # lower-case leg, and an unrecognised value is stored as None rather than
        # rejected — an assignment is still worth having without a declared leg; it
        # simply advances no milestone on release.
        leg = (movement_type or "").strip().upper() or None
        if leg not in MOVEMENTS:
            leg = None
        params = {"call_id": int(call_id), "pilot_code": pilot_code.strip(),
                  "vcn": vcn, "via_no": via_no, "imo_no": imo_no,
                  "vessel_name": vessel_name, "pilot_name": pilot_name,
                  "created_by": created_by, "movement_type": leg,
                  # Only a leg that BERTHS her can carry a destination. An outward movement
                  # is leaving, so a berth sent with it is dropped rather than stored — the
                  # record must not imply a destination the release will never write.
                  "berth_id": (int(berth_id)
                               if berth_id is not None and leg in _MOVEMENT_BERTHS
                               else None)}
        try:
            async with get_engine(self._dsn).begin() as conn:
                # LEGALITY FIRST, inside the transaction. Reading the position here rather
                # than before `begin()` means it cannot change between the check and the
                # insert — a concurrent import that berths her cannot slip past this.
                #
                # An UNDECLARED leg skips the check on purpose: it records no milestone on
                # release, so it can assert nothing impossible, and refusing it would break
                # every pre-0054 client.
                if leg is not None:
                    state = await self._position(conn, int(call_id))
                    allowed = legal_movements(state)
                    if leg not in allowed:
                        raise IllegalMovement(
                            leg, allowed,
                            f"{leg} is not possible for this call: "
                            f"{_ILLEGAL_REASON.get(leg, 'the vessel is not in that state')}")
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
                                        "event_ts": row["assigned_at"],
                                        # Nothing has moved at assignment time.
                                        "berth_id": None})
        except IllegalMovement:
            # Not a precedence refusal — it must reach the caller as its own 422, not be
            # flattened into the 409 that "someone else owns this call" means.
            raise
        except IntegrityError:
            # uq_manual_pilot_assignment_live: this call already has a live assignment.
            # Same refusal as the imported-pilotage case — the caller renders both as 409,
            # because in both the record would be shadowed the moment it was written.
            return None
        return ManualPilotAssignment.from_row(row) if row else None

    @staticmethod
    async def _position(conn: Any, call_id: int) -> "CallProjection":
        """Where this vessel is, derived from her own milestones. Read-only.

        Goes through `project()` rather than calling derive_state directly — the layer has
        exactly one entry point and an architecture test enforces it, so this guard reads
        the SAME lifecycle the operator is looking at and cannot drift from it.

        No manual assignment is passed: this call has none yet (that is what is being
        created), and the merge only ever supplies a pilot_state, which none of the
        legality rules read.

        A call with no events yields the engine's opening state — arrival Pending, not at
        a berth, not in port — under which only an inward movement is legal. Correct for a
        call nothing has happened to yet.
        """
        # Imported HERE, not at module scope: projection.py imports this module for the
        # manual-assignment statuses, so a top-level import is a cycle. Deferring it keeps
        # the single entry point without inverting a dependency that is correct as it is.
        from .projection import project

        call = (await conn.execute(text(_CALL_FOR_STATE),
                                   {"call_id": call_id})).mappings().first()
        events = (await conn.execute(text(_EVENTS_FOR_STATE),
                                     {"call_id": call_id})).mappings().all()
        return project({**(dict(call) if call else {}), "call_id": call_id},
                       [dict(e) for e in events])

    async def board(self, assignment_id: int) -> Optional[ManualPilotAssignment]:
        return await self._advance(_BOARD, assignment_id, EVENT_PILOT_BOARDED,
                                   "boarded_at")

    async def release(self, assignment_id: int) -> Optional[ManualPilotAssignment]:
        """End the movement: the pilot disembarks AND the visit advances.

        The second half is what migration 0054 exists for. Releasing used to record
        PILOT_DISEMBARKED alone, which the status ladder does not read, so a call driven
        through this fallback stayed at 'Pilot Boarded' for ever. The declared leg says
        which visit milestone the disembark completes — see _MOVEMENT_MILESTONE.
        """
        return await self._advance(_RELEASE, assignment_id, EVENT_PILOT_DISEMBARKED,
                                   "released_at", complete_movement=True)

    async def _advance(self, sql: str, assignment_id: int, milestone: str,
                       stamp_col: str, *,
                       complete_movement: bool = False) -> Optional[ManualPilotAssignment]:
        """Advance the assignment AND record the milestone(s), in one transaction.

        The ledger row carries the assignment's own stamp, so the Timeline shows the time
        the operator recorded rather than the time the row was written.

        `complete_movement` records everything the declared leg completed, all inside this
        one transaction so a call can never hold a released assignment with half a
        movement written:

          * the VISIT milestone (BERTHED / SAILED), carrying the destination berth;
          * for OUTWARD, BERTH_VACATED as well — sailing frees the berth, and the engine
            reads `departed or berth_vacated`, so without it a berthed-then-sailed vessel
            occupied her berth for ever;
          * the actual (`ata`/`atd`) the leg completed;
          * for a leg that berths her, core.vessel_call.berth_id — the shift itself.

        Nothing here can overwrite imported history: the milestones are guarded by
        _LEDGER_INSERT's NOT EXISTS and the actual by _STAMP_ACTUAL's `IS NULL`.
        """
        async with get_engine(self._dsn).begin() as conn:
            row = (await conn.execute(text(sql),
                                      {"id": int(assignment_id)})).mappings().first()
            if row is not None:
                leg = row["movement_type"]
                berth_id = row["berth_id"]
                await conn.execute(text(_LEDGER_INSERT),
                                   {"call_id": int(row["call_id"]),
                                    "event_type": milestone,
                                    "event_ts": row[stamp_col],
                                    # The pilot rung itself names no berth; only the visit
                                    # milestone below records where she ended up.
                                    "berth_id": None})
                # None for a leg that was never declared (a pre-0054 row): record nothing
                # and leave the visit exactly where it was, rather than guessing.
                visit = _MOVEMENT_MILESTONE.get(leg)
                if complete_movement and visit is not None:
                    moves = leg in _MOVEMENT_BERTHS and berth_id is not None
                    for event_type in (visit, _MOVEMENT_EXTRA_MILESTONE.get(leg)):
                        if event_type is None:
                            continue
                        await conn.execute(
                            text(_LEDGER_INSERT),
                            {"call_id": int(row["call_id"]),
                             "event_type": event_type,
                             "event_ts": row[stamp_col],
                             # Only the BERTHED rung names a berth — BERTH_VACATED is
                             # about leaving one, and the call still records which.
                             "berth_id": int(berth_id) if moves and event_type == visit
                             else None})
                    # The actuals column the same leg implies. SHIFTING is absent from
                    # _MOVEMENT_ACTUAL on purpose — see the map.
                    col = _MOVEMENT_ACTUAL.get(leg)
                    if col is not None:
                        await conn.execute(
                            text(_STAMP_ACTUAL.format(col=col)),
                            {"call_id": int(row["call_id"]), "ts": row[stamp_col]})
                    # THE SHIFT ITSELF. Without this the release recorded 'she is fast
                    # alongside' while berth_id still named the berth she had just left.
                    if moves:
                        await conn.execute(
                            text(_MOVE_BERTH),
                            {"call_id": int(row["call_id"]), "berth_id": int(berth_id)})
                    log.info("manual pilot release completed movement",
                             extra={"call_id": int(row["call_id"]), "movement_type": leg,
                                    "milestone": visit, "actual": col,
                                    "berth_id": int(berth_id) if moves else None})
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
