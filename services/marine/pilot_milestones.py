"""Pilot-movement milestones -> lifecycle events. Pure, read-only, one implementation.

WHY THIS EXISTS
---------------
core.pilotage has always recorded a seven-stage micro-lifecycle per movement — memo
lodged, anchor down/up, pilot boarded, first line ashore, all fast, pilot away, berth
cleared. The lifecycle engine consumed exactly ONE of them (pilot_boarded_at), so a
mooring the port had timed to the minute appeared in the twin only as 'Pilot Boarded',
and the Timeline showed nothing between boarding and berthing.

Coverage in the client corpus, which is what makes this worth doing:

    submitted_at         423/423   pilot_disembarked_at  324/423
    pilot_boarded_at     423/423   first_line_at         118/423
    all_fast_at          116/423   berth_vacated_at      117/423

NOT A SECOND SOURCE OF TRUTH
----------------------------
These events are SYNTHESISED at read time from columns the importer already wrote. No
parser changes, no import path changes, and nothing is written to the event ledger.
The imported pilot-memo / pilot-card workflow is untouched: this module only reads.

Synthesised events are marked ``source='pilotage'`` so a consumer can tell them from
ledger rows (``source='imported'``) — the Timeline uses that to label provenance without
treating them differently.

DEDUPLICATION
-------------
ANCHORED and PILOT_BOARDED can arrive from BOTH the VESARR/VESDEP ledger and a pilot
card. Emitting both would double the milestone. `merge_events` therefore drops a
synthesised event whose (type, timestamp) a ledger row already carries — the ledger wins,
which is the same imported-first precedence applied everywhere else.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional, Sequence

from .state_engine import (EVENT_ALL_FAST, EVENT_ANCHOR_AWEIGH, EVENT_ANCHORED,
                           EVENT_BERTH_VACATED, EVENT_FIRST_LINE,
                           EVENT_PILOT_BOARDED, EVENT_PILOT_DISEMBARKED,
                           EVENT_PILOT_REQUESTED)

#: pilotage column -> the milestone it records. Order is documentation only; ranking is
#: EVENT_ORDER's job and is never duplicated here.
MILESTONE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("submitted_at", EVENT_PILOT_REQUESTED),
    ("anchor_down_at", EVENT_ANCHORED),
    ("anchor_up_at", EVENT_ANCHOR_AWEIGH),
    ("pilot_boarded_at", EVENT_PILOT_BOARDED),
    ("first_line_at", EVENT_FIRST_LINE),
    ("all_fast_at", EVENT_ALL_FAST),
    ("pilot_disembarked_at", EVENT_PILOT_DISEMBARKED),
    ("berth_vacated_at", EVENT_BERTH_VACATED),
)

#: Marks an event this module produced rather than one read from the event ledger.
SOURCE_PILOTAGE = "pilotage"


def synthesize(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Milestone events for pilotage rows, one per non-null timestamp.

    A call may have several movements (inward, outward, a shift), each contributing its
    own milestones — they are all emitted. Two movements that genuinely reached the same
    milestone at the same instant collapse to one event, because a milestone is a fact
    about the CALL, not about the row that happened to record it.
    """
    seen: set[tuple[str, Any]] = set()
    out: list[dict[str, Any]] = []
    for r in rows or ():
        call_id = r.get("call_id")
        if call_id is None:
            continue  # an unlinked movement belongs to no call's timeline
        for col, event_type in MILESTONE_COLUMNS:
            ts = r.get(col)
            if ts is None:
                continue
            key = (event_type, ts)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "call_id": int(call_id),
                "event_type": event_type,
                "event_ts": ts,
                "berth_id": None,
                "source": SOURCE_PILOTAGE,
                # Which movement produced it — an operator reading the Timeline needs to
                # know whether 'All Fast' was the arrival or a shift.
                "movement_type": r.get("movement_type"),
            })
    return out


def merge_events(ledger: Sequence[Mapping[str, Any]],
                 synthesized: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Ledger rows first, then synthesised ones the ledger does not already carry.

    IMPORTED WINS. A (type, timestamp) the ledger holds is authoritative; the synthesised
    twin is dropped rather than shown twice. Only same-instant duplicates are removed — a
    pilot card recording a boarding one minute after VESARR is a genuine second data point
    and both are kept, because collapsing them would silently discard a discrepancy the
    operator should see.
    """
    have = {(str(e.get("event_type")), e.get("event_ts")) for e in ledger or ()}
    out: list[dict[str, Any]] = [dict(e) for e in ledger or ()]
    for e in synthesized or ():
        if (str(e.get("event_type")), e.get("event_ts")) in have:
            continue
        out.append(dict(e))
    return out


#: Batched read for a page of calls. Selected here so the column list and the milestone
#: map cannot drift apart.
BY_CALL_IDS = (
    "SELECT call_id, movement_type, "
    + ", ".join(col for col, _ in MILESTONE_COLUMNS)
    + " FROM core.pilotage WHERE call_id = ANY(:call_ids)"
)

BY_CALL_ID = (
    "SELECT call_id, movement_type, "
    + ", ".join(col for col, _ in MILESTONE_COLUMNS)
    + " FROM core.pilotage WHERE call_id = :call_id"
)


class PilotMilestoneService:
    """Reads pilotage milestones for the SINGLE-call paths (timeline, state detail).

    The batched list path fetches them inside MarineProjection; this exists so the
    single-call paths do not have to reach into the projection's internals — and so both
    end up calling the same `synthesize`, which is the only place the mapping lives.
    """

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def by_call_id(self, call_id: int) -> list[dict[str, Any]]:
        from sqlalchemy import text  # local: keeps this module importable without a DB

        from jnpa_shared.db import get_engine

        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(BY_CALL_ID),
                                       {"call_id": int(call_id)})).mappings().all()
        return synthesize(rows)


__all__ = ["MILESTONE_COLUMNS", "SOURCE_PILOTAGE", "BY_CALL_IDS", "BY_CALL_ID",
           "PilotMilestoneService", "synthesize", "merge_events"]
