"""Pilotage <- marine lifecycle translation. Mirrors :mod:`services.berthing.lifecycle`.

Turns a :class:`~services.marine.projection.CallProjection` plus the pilotage row's OWN
columns into the pilot workflow vocabulary. It derives NO lifecycle state: every lifecycle
fact is read off the projection, and the only local inputs are pilotage's own
``movement_type`` and timestamps, which belong to this module and are not lifecycle.

WHY THE ROW'S OWN COLUMNS ARE STILL NEEDED
------------------------------------------
The projection describes the CALL. A pilotage row describes ONE MOVEMENT of that call —
inward, outward or a shift — and a single call has several. The projection alone therefore
cannot say whether a completed pilot job was the arrival or the departure one; only
``movement_type`` can. That is a property of the pilotage record, not a second opinion
about the lifecycle, so reading it here duplicates nothing.

SOURCE OF EACH INPUT
--------------------
    projection.pilot_state       PILOT_BOARDED milestone reached / job finished
    projection.departure_state   VESDEP progress
    projection.is_at_berth       BERTHED milestone reached
    row.movement_type            INWARD | OUTWARD | SHIFTING   (pilotage's own)
    row.submitted_at             pilot memo lodged             (pilotage's own)
    row.pilot_boarded_at         actual boarding               (pilotage's own)
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from .projection import CallProjection

# The pilot workflow vocabulary. Ordered, lowest first.
PLANNED = "Planned"
REQUESTED = "Pilot Requested"
BOARDED = "Pilot Boarded"
COMPLETED = "Pilot Completed"
DEPARTURE_COMPLETED = "Departure Pilot Completed"

WORKFLOW: tuple[str, ...] = (PLANNED, REQUESTED, BOARDED, COMPLETED, DEPARTURE_COMPLETED)

_OUTWARD = "OUTWARD"


def derive(row: Mapping[str, Any],
           state: Optional[CallProjection] = None) -> str:
    """Pilot workflow status for ONE pilotage movement.

    Works with no projection at all — a pilot card can be imported before its PCS call
    exists, and must still show a sensible workflow position from its own timestamps.
    The projection, when present, is what lets a movement complete on the strength of the
    call's milestones rather than only the card's own columns.
    """
    movement = str(row.get("movement_type") or "").strip().upper()
    boarded_at = row.get("pilot_boarded_at")
    disembarked_at = row.get("pilot_disembarked_at")

    # --- finished? -------------------------------------------------------------------
    # The card says so, or the call's lifecycle does.
    job_done = disembarked_at is not None
    if state is not None and not job_done:
        job_done = state.pilot_state == "Completed"
    if job_done:
        # Only movement_type can say WHICH pilot job finished.
        return DEPARTURE_COMPLETED if movement == _OUTWARD else COMPLETED

    # --- boarded? --------------------------------------------------------------------
    if boarded_at is not None:
        return BOARDED
    if state is not None and state.pilot_state == "Active":
        return BOARDED

    # --- requested? ------------------------------------------------------------------
    # The pilot memo was lodged but no boarding is recorded yet.
    if row.get("submitted_at") is not None:
        return REQUESTED

    return PLANNED


def effective_times(row: Mapping[str, Any],
                    state: Optional[CallProjection] = None) -> dict[str, Any]:
    """Actual timestamps for this movement, card first, projection as the fallback.

    The card is preferred because it is the pilot's own record of the movement; the
    projection fills a gap the card left blank (a VESARR/VESDEP milestone the pilot card
    never captured). Only keys with a value are returned, so an absent time stays absent
    rather than becoming null noise.
    """
    out: dict[str, Any] = {}
    boarded = row.get("pilot_boarded_at") or (state.pilot_boarded_at if state else None)
    if boarded is not None:
        out["pilot_boarded_at"] = boarded
    if row.get("all_fast_at") is not None:
        out["all_fast_at"] = row["all_fast_at"]
    elif state is not None and state.berthed_at is not None:
        out["all_fast_at"] = state.berthed_at
    if row.get("pilot_disembarked_at") is not None:
        out["pilot_disembarked_at"] = row["pilot_disembarked_at"]
    if state is not None and state.departed_at is not None:
        out["departed_at"] = state.departed_at
    return out


def apply(row: Mapping[str, Any],
          state: Optional[CallProjection] = None) -> dict[str, Any]:
    """Return the pilotage row with its workflow status attached under ``extras``.

    SHAPE-PRESERVING: the row keeps every key it arrived with and gains none. ``extras`` is
    an existing open ``jsonb`` field — the one place a derived value can live without
    altering PilotageOut — and the addition is namespaced under ``lifecycle`` so it can
    never collide with a sheet column the parser promoted there.
    """
    out = dict(row)
    extras = dict(out.get("extras") or {})
    block: dict[str, Any] = {"pilot_status": derive(row, state)}
    block.update(effective_times(row, state))
    if state is not None:
        block["call_id"] = state.call_id
        block["call_status"] = state.status
    extras["lifecycle"] = block
    out["extras"] = extras
    return out
