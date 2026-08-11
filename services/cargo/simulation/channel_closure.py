"""Scenario N-1 — Channel Closure and Berth-Lock (bidder-proposed).

    "The approach channel is lost for N hours — a grounding, a sunken craft, an
     emergency survey. Arrivals AND sailings stop together. At what hour does the
     port become berth-locked, and in what order should the held vessels sail
     when the channel reopens?"

Why this scenario
-----------------
Every scenario in the briefing and the Notice throttles **one** flow: weather
delays arrivals, a crane fault slows discharge, a driver shortage slows
evacuation. A channel closure throttles **both directions through one shared
asset**, and that is a different shape of problem.

The consequence is not additive, it is compounding. Vessels that finish working
cannot leave, so their berths never free; vessels waiting cannot enter, so the
queue builds outside. The port reaches a state where **no berth can be released
by any action available inside the port** — berth-lock — and from that moment
every additional hour of closure costs a full berth-hour across the estate.

The question "when does the port stop being able to help itself" is not asked
anywhere else in the catalogue, and it is the one a Deputy Conservator needs
before deciding whether a closure is tolerable or must be fought.

Method
------
1. Load the calls contending for berths over the window (projected forward when
   the study day is beyond the corpus — assumption A-07).
2. Establish the berth pool from the berths those calls occupy.
3. Walk the closure hour by hour. A berth is:

   * **working**  — its vessel is still in its operation window
   * **held**     — its vessel finished working but cannot sail (channel shut)
   * **free**     — neither

4. **Berth-lock** is the first hour at which no berth is free and at least one is
   held: nothing can enter, and nothing can leave to make room.
5. On reopening, the held vessels sail in the proposed order. The default is
   longest-held first (the fairest reading of a queue), and the response reports
   what the alternative — largest vessel first, to free the deepest berth —
   costs against the same measure.

Honesty rules
-------------
* Channel transit time is a declared input; the corpus carries no pilotage or
  transit record. It is stated, not buried.
* Vessels are not permitted to sail early to beat the closure. Modelling that
  would need a decision rule nobody has committed to, and it would flatter the
  answer.
* Berth-lock is reported as ``null`` when it is never reached, never as the end
  of the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_PARAMETER, SimulationResult, pct)
from .projection import load_calls_for

SCENARIO = "channel-closure"

DEFAULT_CLOSURE_HOURS = 12
#: One-way transit between the pilot boarding ground and the berths. Declared:
#: the corpus carries no pilotage timing.
DEFAULT_TRANSIT_HOURS = 1.5


def _op_start(call: dict) -> Optional[datetime]:
    for key in ("cargo_operation_start", "berthing_time", "ata", "eta"):
        if call.get(key) is not None:
            return call[key]
    return None


def _op_end(call: dict) -> Optional[datetime]:
    for key in ("cargo_operation_end", "departure_time"):
        if call.get(key) is not None:
            return call[key]
    return None


def berth_states(calls: list[dict], at: datetime) -> dict[str, str]:
    """Berth -> working | held | free at one instant, under closure.

    ``held`` is the state that matters: the vessel has finished and would
    otherwise have sailed, but the channel is shut, so the berth is occupied by a
    vessel doing nothing."""
    states: dict[str, str] = {}
    for call in calls:
        berth = call.get("berth_number") or "UNASSIGNED"
        start, end = _op_start(call), _op_end(call)
        if start is None or start > at:
            continue
        if end is None or end > at:
            states[berth] = "working"
        elif states.get(berth) != "working":
            states[berth] = "held"
    return states


def walk_closure(calls: list[dict], berths: list[str], *,
                 closure_from: datetime, closure_to: datetime) -> list[dict]:
    """Hour-by-hour berth occupancy through the closure. Pure, deterministic."""
    rows: list[dict] = []
    hour = closure_from
    while hour < closure_to:
        states = berth_states(calls, hour)
        working = sum(1 for b in berths if states.get(b) == "working")
        held = sum(1 for b in berths if states.get(b) == "held")
        free = len(berths) - working - held
        rows.append({
            "bucket": hour,
            "working": working,
            "held": held,
            "free": free,
            "berth_locked": free == 0 and held > 0,
        })
        hour += timedelta(hours=1)
    return rows


def sailing_order(held: list[dict], policy: str) -> list[dict]:
    """Order the held vessels sail in once the channel reopens."""
    if policy == "LARGEST_FIRST":
        return sorted(held, key=lambda c: (-(c.get("gross_moves") or 0),
                                           str(c.get("vessel_name") or "")))
    # LONGEST_HELD_FIRST — the vessel that finished earliest has waited longest.
    return sorted(held, key=lambda c: (_op_end(c) or datetime.max,
                                       str(c.get("vessel_name") or "")))


async def run(repo: Any, params: dict) -> SimulationResult:
    """``params``: as_of (required); closure_hours, transit_hours, terminal,
    horizon_hours."""
    as_of: datetime = params["as_of"]
    closure_hours = float(params.get("closure_hours", DEFAULT_CLOSURE_HOURS))
    transit_hours = float(params.get("transit_hours", DEFAULT_TRANSIT_HOURS))
    terminal = (params.get("terminal") or "").strip() or None
    horizon_hours = int(params.get("horizon_hours", max(48, closure_hours * 2)))
    closure_to = as_of + timedelta(hours=closure_hours)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Walk the closure hour by hour over the berths the calls occupy. A "
            "berth is working while its vessel is inside its operation window, "
            "held once the vessel has finished but cannot sail, and free "
            "otherwise. Berth-lock is the first hour with no free berth and at "
            "least one held: nothing can enter and nothing can leave to make "
            "room. On reopening the held vessels sail in the proposed order, and "
            "an alternative order is costed against the same measure."),
    )
    res.assume("closure_hours", closure_hours,
               "the length of the channel outage under study", SOURCE_PARAMETER)
    res.assume("transit_hours", transit_hours,
               "one-way channel transit between the pilot boarding ground and the "
               "berths. The corpus carries no pilotage or transit record, so this "
               "is a declared figure.", SOURCE_ASSUMED)
    res.assume("no_early_sailing", True,
               "vessels are not allowed to sail early to beat the closure; no such "
               "decision rule exists in the data and assuming one would flatter "
               "the result.", SOURCE_ASSUMED)

    calls, coverage = await load_calls_for(
        repo, res, as_of=as_of, horizon_hours=horizon_hours, terminal=terminal)
    if not calls:
        res.result = {"coverage": coverage.to_dict()}
        return res

    berths = sorted({c.get("berth_number") or "UNASSIGNED" for c in calls})
    timeline = walk_closure(calls, berths, closure_from=as_of,
                            closure_to=closure_to)
    locked = next((r for r in timeline if r["berth_locked"]), None)
    lock_hours = (round((locked["bucket"] - as_of).total_seconds() / 3600.0, 1)
                  if locked else None)

    held_calls = [c for c in calls
                  if (_op_end(c) is not None and _op_end(c) <= closure_to
                      and _op_end(c) >= as_of)]
    recommended = sailing_order(held_calls, "LONGEST_HELD_FIRST")
    alternative = sailing_order(held_calls, "LARGEST_FIRST")

    def clearance_hours(order: list[dict]) -> float:
        """Hours to get every held vessel out, one transit slot at a time."""
        return round(len(order) * transit_hours, 2)

    def total_held_hours(order: list[dict]) -> float:
        """Sum of hours each vessel spends held, under this sailing order."""
        total = 0.0
        for position, call in enumerate(order):
            finished = _op_end(call) or as_of
            sails_at = closure_to + timedelta(hours=transit_hours * position)
            total += max(0.0, (sails_at - finished).total_seconds() / 3600.0)
        return round(total, 2)

    res.result = {
        "coverage": coverage.to_dict(),
        "closure": {"from": as_of, "to": closure_to, "hours": closure_hours},
        "berth_pool": berths,
        "timeline": timeline,
        "berth_locked_at": locked["bucket"] if locked else None,
        "held_vessels": [
            {"vessel": c.get("vessel_name"), "voyage": c.get("voyage_number"),
             "berth": c.get("berth_number"), "terminal": c.get("terminal"),
             "finished_at": _op_end(c), "gross_moves": c.get("gross_moves")}
            for c in recommended],
        "sailing_order": {
            "recommended": {
                "policy": "LONGEST_HELD_FIRST",
                "sequence": [c.get("vessel_name") for c in recommended],
                "total_held_hours": total_held_hours(recommended),
                "clearance_hours": clearance_hours(recommended)},
            "alternative": {
                "policy": "LARGEST_FIRST",
                "sequence": [c.get("vessel_name") for c in alternative],
                "total_held_hours": total_held_hours(alternative),
                "clearance_hours": clearance_hours(alternative),
                "cost_vs_recommended": round(
                    total_held_hours(alternative) - total_held_hours(recommended), 2)},
        },
    }
    res.figures = {
        "berths_in_pool": len(berths),
        "vessels_in_window": len(calls),
        "berth_lock_reached": locked is not None,
        "hours_to_berth_lock": lock_hours,
        "berths_held_at_reopen": timeline[-1]["held"] if timeline else 0,
        "berths_free_at_reopen": timeline[-1]["free"] if timeline else 0,
        "vessels_held": len(held_calls),
        "total_held_hours": total_held_hours(recommended),
        "clearance_hours_after_reopen": clearance_hours(recommended),
        "alternative_order_costs_hours": round(
            total_held_hours(alternative) - total_held_hours(recommended), 2),
        "berth_hours_lost": round(sum(r["held"] for r in timeline), 1),
        "peak_berth_utilisation_pct": (
            pct(max((r["working"] + r["held"] for r in timeline), default=0),
                len(berths)) if berths else 0.0),
    }

    if locked:
        res.recommend(
            "ESCALATE_CHANNEL_CLEARANCE",
            f"the port is berth-locked {lock_hours}h into the closure — from that "
            "point no berth can be released by any action inside the port, so "
            "every further hour costs the full estate",
            hours_to_lock=lock_hours)
    else:
        res.recommend(
            "CLOSURE_TOLERABLE",
            f"berth-lock is not reached within {closure_hours:g}h; "
            f"{timeline[-1]['free'] if timeline else 0} berth(s) remain free at "
            "reopening",
            free_at_reopen=timeline[-1]["free"] if timeline else 0)
    if held_calls:
        res.recommend(
            "SEQUENCE_OUTBOUND_CONVOY",
            f"{len(held_calls)} vessel(s) are held at reopening; sailing "
            f"longest-held first costs {total_held_hours(recommended)}h against "
            f"{total_held_hours(alternative)}h for largest-first",
            vessels=len(held_calls))
    return res
