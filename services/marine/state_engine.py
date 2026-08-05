"""UC-I Marine Business State Engine — the ONE place that understands call progression.

Pure: no DB, no I/O, no SQL execution. It owns three things that were previously scattered
or re-declared, and every other marine module now imports them from here rather than
restating them:

  1. EVENT_ORDER   — the milestone ladder and its rank.
  2. STATUS_ORDER  — the call-stage ladder (vocabulary itself stays in parsers/pcs_common).
  3. in_port_sql() — the single definition of "in port", previously written out three times
                     in repository.py.

and it derives the operational state of one call from ``core.vessel_call`` +
``core.vessel_call_event`` via :func:`derive_state`.

WHY RANK, NOT TIMESTAMP
-----------------------
The corpus does not order cleanly by time. ARRIVED and BERTHED carry the SAME timestamp on
every verified VESARR call (21:24/21:24, 12:00/12:00), and SAILED PRECEDES DEPARTED
(02:15 vs 04:40). Sorting milestones by ``event_ts`` alone therefore produces a
non-deterministic "latest" on ties and would report a vessel as sailing before it departed.
The ladder below is the authority; ``event_ts`` is only the tiebreak within a rank.

SCOPE
-----
Adds no table, no column, no endpoint, no response field. Nothing here changes what an
existing query returns — repository.py imports the constants it used to declare inline.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from .parsers.pcs_common import (CALL_STATUS_BERTH_ALLOTTED, CALL_STATUS_BERTH_PLANNED,
                                 CALL_STATUS_PLANNED, CALL_STATUS_VCN_ALLOTTED)

# --------------------------------------------------------------------------- milestones
EVENT_BERTH_ALLOTTED = "BERTH_ALLOTTED"
EVENT_PILOT_REQUESTED = "PILOT_REQUESTED"
EVENT_ANCHORED = "ANCHORED"
EVENT_ANCHOR_AWEIGH = "ANCHOR_AWEIGH"
EVENT_PILOT_BOARDED = "PILOT_BOARDED"
EVENT_CRAFT_ASSIGNED = "CRAFT_ASSIGNED"
EVENT_CRAFT_DISPATCHED = "CRAFT_DISPATCHED"
EVENT_CRAFT_ON_SCENE = "CRAFT_ON_SCENE"
EVENT_CRAFT_ASSISTING = "CRAFT_ASSISTING"
EVENT_CRAFT_RELEASED = "CRAFT_RELEASED"
EVENT_FIRST_LINE = "FIRST_LINE"
EVENT_ALL_FAST = "ALL_FAST"
EVENT_BERTHED = "BERTHED"
EVENT_ARRIVED = "ARRIVED"
EVENT_PILOT_DISEMBARKED = "PILOT_DISEMBARKED"
EVENT_BERTH_VACATED = "BERTH_VACATED"
EVENT_SAILED = "SAILED"
EVENT_DEPARTED = "DEPARTED"

#: The business lifecycle, lowest rank first. Index == rank.
#:
#: The five marked NEW are the pilot-movement milestones core.pilotage has always
#: recorded — first line ashore, all fast, pilot away, berth cleared — and which this
#: engine previously ignored, so a mooring that the port had timed to the minute showed
#: in the twin only as 'Pilot Boarded'. They are SYNTHESISED from those columns by
#: services.marine.pilot_milestones; no parser and no import path changed.
#:
#: Placement is operational truth, not convenience: lines go ashore before the vessel is
#: fast, the pilot leaves after that, and the berth is cleared before the vessel sails.
EVENT_ORDER: tuple[str, ...] = (
    EVENT_BERTH_ALLOTTED,     # BERALT
    EVENT_PILOT_REQUESTED,    # NEW — pilotage.submitted_at (memo lodged)
    EVENT_ANCHORED,           # VESARR / pilotage.anchor_down_at
    EVENT_ANCHOR_AWEIGH,      # NEW — pilotage.anchor_up_at
    EVENT_PILOT_BOARDED,      # VESARR / VESDEP / pilotage.pilot_boarded_at
    EVENT_FIRST_LINE,         # NEW — pilotage.first_line_at
    EVENT_ALL_FAST,           # NEW — pilotage.all_fast_at
    EVENT_BERTHED,            # VESARR
    EVENT_ARRIVED,            # VESARR  (same ts as BERTHED in the corpus)
    # ---- craft dispatch, ABOVE the mooring milestones ------------------------------
    # A craft movement is routinely ordered against a vessel that is ALREADY alongside
    # (a shift, an outbound job), so ranking these below BERTHED would leave
    # `latest_event` stuck on BERTHED after a dispatch — the operator would see stale
    # progress. They stay BELOW sailing and departure, which genuinely end the call and
    # must never be masked by a tug standing down.
    EVENT_CRAFT_ASSIGNED,
    EVENT_CRAFT_DISPATCHED,
    EVENT_CRAFT_ON_SCENE,
    EVENT_CRAFT_ASSISTING,
    EVENT_CRAFT_RELEASED,
    EVENT_PILOT_DISEMBARKED,  # NEW — pilotage.pilot_disembarked_at
    EVENT_BERTH_VACATED,      # NEW — pilotage.berth_vacated_at
    EVENT_SAILED,             # VESDEP  (earlier ts than DEPARTED in the corpus)
    EVENT_DEPARTED,           # VESDEP  — the official ATD milestone
)

#: Call-stage ladder. The vocabulary lives in parsers/pcs_common; only the ORDER is here.
STATUS_ORDER: tuple[str, ...] = (
    CALL_STATUS_PLANNED,          # CALINF
    CALL_STATUS_VCN_ALLOTTED,     # CALINV
    CALL_STATUS_BERTH_PLANNED,    # BERMAN
    CALL_STATUS_BERTH_ALLOTTED,   # BERALT
)

# Operational statuses the ENGINE derives once milestones exist. They extend the ladder
# above; they are not written to core.vessel_call by any parser.
STATUS_ANCHORED = "Anchored"
STATUS_PILOT_BOARDED = "Pilot Boarded"
STATUS_AT_BERTH = "At Berth"
STATUS_SAILING = "Sailing"
STATUS_DEPARTED = "Departed"

_UNKNOWN_RANK = -1


def event_rank(event_type: Optional[str]) -> int:
    """Ladder position of a milestone; -1 when unrecognised.

    -1 (not 0) so an unknown milestone can never outrank BERTH_ALLOTTED and silently
    become the 'latest' event.
    """
    if not event_type:
        return _UNKNOWN_RANK
    try:
        return EVENT_ORDER.index(str(event_type).strip().upper())
    except ValueError:
        return _UNKNOWN_RANK


def status_rank(status: Optional[str]) -> int:
    """Ladder position of a call stage; -1 when unrecognised (legacy free-text, NULL)."""
    if not status:
        return _UNKNOWN_RANK
    try:
        return STATUS_ORDER.index(str(status).strip())
    except ValueError:
        return _UNKNOWN_RANK


# --------------------------------------------------------------------------- SQL fragments
def in_port_sql(alias: str = "c") -> str:
    """The single definition of "in port": arrived and not yet departed.

    Previously written out verbatim at three sites in repository.py (the `in_port` filter
    and both stats aggregates), so a change to the rule had to be made three times.
    """
    return f"{alias}.ata IS NOT NULL AND {alias}.atd IS NULL"


def status_order_sql() -> str:
    """The call-stage ladder as a PostgreSQL text[] literal, for the monotonicity guard.

    Rendered from STATUS_ORDER so the SQL guard and the Python engine can never disagree
    about the order — that divergence is exactly what this module exists to prevent.
    """
    inner = ",".join("'" + s.replace("'", "''") + "'" for s in STATUS_ORDER)
    return f"ARRAY[{inner}]"


# --------------------------------------------------------------------------- return model
@dataclass(frozen=True)
class CallState:
    """Operational state of ONE vessel call, derived from its stage + milestones."""
    status: Optional[str]
    arrival_state: str
    berth_state: str
    pilot_state: str
    departure_state: str
    shipping_state: str
    portcraft_state: str
    craft_state: str
    is_in_port: bool
    is_at_berth: bool
    latest_event: Optional[str]
    latest_event_time: Optional[datetime]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _reached(ranks: set[int], event_type: str) -> bool:
    return event_rank(event_type) in ranks


def derive_state(call: Mapping[str, Any],
                 events: Iterable[Mapping[str, Any]] = ()) -> CallState:
    """Derive the operational state of one call.

    :param call:   a ``core.vessel_call`` row (only ``status`` is read).
    :param events: its ``core.vessel_call_event`` rows, any order.

    Milestones are treated as a REACHED SET rather than a sequence: the corpus re-emits
    the same message (VESDEP appears twice for one call) and ties timestamps, so "has this
    happened" is decidable while "what happened last by clock" is not. The reported
    ``latest_event`` is the highest-RANK milestone reached, with ``event_ts`` breaking
    ties within a rank.
    """
    ranked: list[tuple[int, Optional[datetime], Optional[str]]] = []
    for e in events or ():
        et = e.get("event_type")
        r = event_rank(et)
        if r == _UNKNOWN_RANK:
            continue  # unrecognised milestone: never advances state
        ranked.append((r, e.get("event_ts"), et))

    reached = {r for r, _, _ in ranked}
    # Earliest time per milestone. The reached-SET answers "did this happen"; a few rules
    # also need "in what order", and only these timestamps can say.
    first: dict[str, Optional[datetime]] = {}
    for _, ts, et in ranked:
        if ts is None or et is None:
            continue
        cur = first.get(et)
        if cur is None or ts < cur:
            first[et] = ts
    latest_event: Optional[str] = None
    latest_time: Optional[datetime] = None
    if ranked:
        top = max(r for r, _, _ in ranked)
        same = [(ts, et) for r, ts, et in ranked if r == top]
        # Tiebreak within a rank by time; None sorts first so a timed row wins.
        same.sort(key=lambda x: (x[0] is not None, x[0]))
        latest_time, latest_event = same[-1]

    anchored = _reached(reached, EVENT_ANCHORED)
    piloted = _reached(reached, EVENT_PILOT_BOARDED)
    berthed = _reached(reached, EVENT_BERTHED)
    arrived = _reached(reached, EVENT_ARRIVED)
    sailed = _reached(reached, EVENT_SAILED)
    departed = _reached(reached, EVENT_DEPARTED)
    allotted = _reached(reached, EVENT_BERTH_ALLOTTED)
    craft_assigned = _reached(reached, EVENT_CRAFT_ASSIGNED)
    craft_released = _reached(reached, EVENT_CRAFT_RELEASED)
    craft_dispatched = _reached(reached, EVENT_CRAFT_DISPATCHED)
    craft_on_scene = _reached(reached, EVENT_CRAFT_ON_SCENE)
    craft_assisting = _reached(reached, EVENT_CRAFT_ASSISTING)
    first_line = _reached(reached, EVENT_FIRST_LINE)
    all_fast = _reached(reached, EVENT_ALL_FAST)
    disembarked = _reached(reached, EVENT_PILOT_DISEMBARKED)
    berth_vacated = _reached(reached, EVENT_BERTH_VACATED)

    # --- status: the parser-set stage, extended once milestones exist -----------------
    stage = call.get("status")
    if departed:
        status = STATUS_DEPARTED
    elif sailed:
        status = STATUS_SAILING
    elif berthed or arrived:
        status = STATUS_AT_BERTH
    elif piloted:
        status = STATUS_PILOT_BOARDED
    elif anchored:
        status = STATUS_ANCHORED
    else:
        status = stage

    # --- per-module states -------------------------------------------------------------
    # A berthed vessel has necessarily arrived, so BERTHED completes arrival even when the
    # ARRIVED milestone is absent (the two share a timestamp in the corpus).
    if arrived or berthed:
        arrival_state = "Completed"
    elif anchored:
        arrival_state = "Anchored"
    else:
        arrival_state = "Pending"

    # BERTH_VACATED is the berth's own release signal — it precedes SAILED, so a vessel
    # that has cleared its berth frees it without waiting for the departure message.
    if departed or berth_vacated:
        berth_state = "Released"
    # ALL_FAST means moored, which is what 'Occupied' has always meant; the corpus simply
    # never surfaced it. FIRST_LINE is the transition between the two.
    elif berthed or all_fast:
        berth_state = "Occupied"
    elif first_line:
        berth_state = "Mooring"
    elif berthed:
        berth_state = "Occupied"
    elif allotted:
        berth_state = "Allotted"
    else:
        berth_state = "Pending"

    # The pilot's job ends when the vessel is fast alongside (inbound) or clear (outbound).
    # An explicit disembark time says so directly and outranks every inference below.
    # A berthing or all-fast completes the pilot's job ONLY IF IT FOLLOWED THE BOARDING —
    # then the two describe one inbound movement and the pilot is done.
    #
    # Membership alone is not enough. A vessel already alongside that takes a pilot for a
    # departure or a shift satisfies "boarded AND berthed" the instant the pilot steps
    # aboard, and the old rule declared the job finished before it began. That is wrong for
    # imported data too: 18 calls in the client corpus record a boarding AFTER their
    # berthing, and every one was reported Completed while the pilot was still working.
    #
    # Ties still complete: the corpus routinely stamps ARRIVED/BERTHED at the same instant
    # as the milestone before them, so `>=` preserves the inbound case exactly.
    def _completed_by(milestone: str) -> bool:
        m, b = first.get(milestone), first.get(EVENT_PILOT_BOARDED)
        if not _reached(reached, milestone):
            return False
        if m is None or b is None:
            return True   # untimed rows: fall back to membership, as before
        return m >= b

    if disembarked:
        pilot_state = "Completed"
    elif piloted and (_completed_by(EVENT_BERTHED) or _completed_by(EVENT_ALL_FAST)
                      or departed):
        pilot_state = "Completed"
    elif piloted:
        pilot_state = "Active"
    else:
        pilot_state = "Pending"

    if departed:
        departure_state = "Completed"
    elif sailed:
        departure_state = "Sailing"
    else:
        departure_state = "Pending"

    is_in_port = (arrived or berthed) and not departed
    is_at_berth = berthed and not departed

    if departed:
        shipping_state = "Sailed"
    elif is_in_port:
        shipping_state = "In Port"
    else:
        shipping_state = "Expected"

    # Craft are engaged from pilot boarding until the vessel is fast, and again from
    # sailing until departure. Derived from the call lifecycle only — this engine reads no
    # port-craft table, and says nothing about individual tug availability.
    craft_engaged = (piloted and not berthed) or berthed or (sailed and not departed)
    portcraft_state = "Busy" if (craft_engaged and not departed) else "Idle"

    # SUPPLY, not demand. `portcraft_state` above says whether this movement REQUIRES
    # craft; `craft_state` says whether any are actually committed to it. Conflating them
    # would make a commitment look like a requirement and hide an uncovered movement.
    #
    # Derived from the ledger like every other state: CRAFT_RELEASED is written only when
    # the LAST craft stands down, and re-committing clears it, so the two milestones are
    # never both current. Departure ends any engagement regardless.
    # Highest rung REACHED, mirroring how every other state here is derived. With several
    # craft on one movement that is the most advanced stage any of them has reached — the
    # call's engagement is as far along as its furthest craft.
    if departed or craft_released or not craft_assigned:
        craft_state = "Idle"
    elif craft_assisting:
        craft_state = "Assisting"
    elif craft_on_scene:
        craft_state = "OnScene"
    elif craft_dispatched:
        craft_state = "Dispatched"
    else:
        craft_state = "Committed"

    return CallState(
        status=status,
        arrival_state=arrival_state,
        berth_state=berth_state,
        pilot_state=pilot_state,
        departure_state=departure_state,
        shipping_state=shipping_state,
        portcraft_state=portcraft_state,
        craft_state=craft_state,
        is_in_port=bool(is_in_port),
        is_at_berth=bool(is_at_berth),
        latest_event=latest_event,
        latest_event_time=latest_time,
    )


__all__ = [
    "EVENT_ORDER", "STATUS_ORDER", "CallState", "derive_state",
    "event_rank", "status_rank", "in_port_sql", "status_order_sql",
    "EVENT_BERTH_ALLOTTED", "EVENT_ANCHORED", "EVENT_PILOT_BOARDED", "EVENT_BERTHED",
    "EVENT_PILOT_REQUESTED", "EVENT_ANCHOR_AWEIGH", "EVENT_FIRST_LINE", "EVENT_ALL_FAST",
    "EVENT_PILOT_DISEMBARKED", "EVENT_BERTH_VACATED",
    "EVENT_CRAFT_ASSIGNED", "EVENT_CRAFT_DISPATCHED", "EVENT_CRAFT_ON_SCENE",
    "EVENT_CRAFT_ASSISTING", "EVENT_CRAFT_RELEASED",
    "EVENT_ARRIVED", "EVENT_SAILED", "EVENT_DEPARTED",
    "STATUS_ANCHORED", "STATUS_PILOT_BOARDED", "STATUS_AT_BERTH", "STATUS_SAILING",
    "STATUS_DEPARTED",
]
