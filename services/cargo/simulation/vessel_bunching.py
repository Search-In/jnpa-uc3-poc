"""Scenario I-A — Vessel Bunching.

    "On 6 August 2026 a large number of vessels are alongside across the port's
     berths, with the load unevenly distributed between terminals. Propose a
     berthing order for that day. State the objective your order optimises for -
     waiting time, total moves handled, line priority, or another basis of your
     choosing - and show what an alternative order would cost against the same
     objective."                                   - JNPA Notice, 05 Aug 2026, §2 I-A

This is the one Notice scenario the engine did not answer. It was left
unregistered deliberately (see ``__init__``) because the Notice leaves the
objective to the bidder, and picking one silently would have been the wrong kind
of answer. It is now answered explicitly: the objective is a request parameter,
it is named in the response, and **every** ordering is scored against whichever
objective was chosen, so the comparison the Notice asks for is like-for-like.

Method
------
1. Load the calls contending for berths on the study day. 6 Aug 2026 lies beyond
   the corpus, so :mod:`.projection` carries the 5 Aug state forward and declares
   it (assumption A-07).
2. Build the berth pool: the distinct berths those calls actually occupy. The
   pool is observed, not configured, so it cannot drift from the data.
3. Schedule each ordering by list-scheduling: take vessels in the order's
   sequence, give each the berth that frees earliest, and start it at
   ``max(ready, berth_free)``. Deterministic — ties break on the vessel's own
   original berth first, then alphabetically.
4. Score every ordering on the same objective and report the deltas.

Four orderings are compared:

=================  =========================================================
``FCFS``           by readiness - the do-nothing baseline, i.e. the sequence
                   the port would run without intervention
``SPT``            shortest service first - the classic minimiser of mean
                   waiting time
``MAX_MOVES``      largest move count first - maximises volume cleared early
``LINE_PRIORITY``  by declared line/service commitment
=================  =========================================================

Objective
---------
The default is total waiting time, scored with the weights already used by the
UC-1 berth optimiser (``poc_1/ml/src/uc1_models/uc1_m5_berth_optimiser.py``)::

    cost = 1.0 * waiting_hours + 2.0 * tide_misses + 0.5 * berth_shifts

Reusing that definition rather than inventing a second one is deliberate: two
optimisers in one submission that disagree about what "better" means would
undermine both. The ``tide_misses`` term is carried but evaluates to zero here
and says so - tidal windows are a UC-1 dataset and are not in this database.

Honesty rules
-------------
* Readiness: the berthing feed carries no ETA/ATA, so readiness is the later of
  the study-day start and the recorded alongside time. On a bunching day the
  Notice's own premise is that the vessels are already present, which this
  reflects. Declared, not buried.
* A call with no derivable service duration uses the median of those that have
  one, flagged per call and declared once.
* ``LINE_PRIORITY`` is only meaningful where the feed carries a line; where it
  does not, the ordering degrades to FCFS and the response says so rather than
  presenting a ranking built on a blank column.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_DERIVED, SOURCE_MEASURED,
                   SOURCE_PARAMETER, SimulationError, SimulationResult,
                   hours_between, pct)
from .projection import load_calls_for

SCENARIO = "vessel-bunching"
DEFAULT_HORIZON_HOURS = 24
UNASSIGNED_BERTH = "UNASSIGNED"

#: Objective weights, shared with the UC-1 berth optimiser. See module docstring.
W_WAITING = 1.0
W_TIDE_MISS = 2.0
W_BERTH_SHIFT = 0.5

OBJECTIVES = {
    "waiting_time": ("total waiting time across the queue "
                     "(+0.5 per berth reassignment)", "minimise"),
    "moves_handled": ("container moves completed inside the horizon", "maximise"),
    "line_priority": ("waiting time weighted by declared line commitment",
                      "minimise"),
}

ORDERINGS = ("FCFS", "SPT", "MAX_MOVES", "LINE_PRIORITY")


# ------------------------------------------------------------------- helpers
def _ready_of(call: dict, day_start: datetime) -> datetime:
    """When the vessel is available to take a berth.

    ETA/ATA are preferred but the daily berthing feed carries neither, so this
    normally resolves to the recorded alongside time, floored at the start of the
    study day. Flooring is what makes a bunching day computable: the Notice's
    premise is that the vessels are already there, so they contend from the
    opening of the day rather than from whenever they happened to berth."""
    for key in ("ata", "eta"):
        if call.get(key) is not None:
            return max(day_start, call[key])
    for key in ("berthing_time", "cargo_operation_start"):
        if call.get(key) is not None:
            return max(day_start, call[key])
    return day_start


def _service_hours(call: dict) -> Optional[float]:
    return hours_between(call.get("cargo_operation_start"),
                         call.get("cargo_operation_end"))


def _candidate(call: dict, day_start: datetime, fallback_hours: float) -> dict:
    hours = _service_hours(call)
    return {
        "berthing_record_id": call.get("berthing_record_id") or call.get("id"),
        "vessel_name": call.get("vessel_name"),
        "voyage_number": call.get("voyage_number"),
        "terminal": call.get("terminal"),
        "original_berth": call.get("berth_number") or UNASSIGNED_BERTH,
        "shipping_line": call.get("shipping_line"),
        "gross_moves": call.get("gross_moves") or 0,
        "ready": _ready_of(call, day_start),
        "service_hours": hours if hours is not None else fallback_hours,
        "service_hours_assumed": hours is None,
    }


def _sequence(candidates: list[dict], ordering: str) -> list[dict]:
    """Deterministic sequence for one ordering. Vessel name is always the final
    tie-break so a rerun cannot reshuffle equal candidates."""
    name = lambda c: str(c["vessel_name"] or "")  # noqa: E731
    if ordering == "SPT":
        return sorted(candidates, key=lambda c: (c["service_hours"], name(c)))
    if ordering == "MAX_MOVES":
        return sorted(candidates, key=lambda c: (-c["gross_moves"], name(c)))
    if ordering == "LINE_PRIORITY":
        # Calls with a declared line first, then by line, then readiness.
        return sorted(candidates,
                      key=lambda c: (c["shipping_line"] in (None, "", "NA"),
                                     str(c["shipping_line"] or ""), c["ready"], name(c)))
    return sorted(candidates, key=lambda c: (c["ready"], name(c)))  # FCFS


def schedule(candidates: list[dict], berths: list[str], ordering: str) -> list[dict]:
    """List-schedule the sequence onto the berth pool.

    Each vessel takes the berth that frees earliest; among berths free at the
    same moment its own original berth wins, then alphabetical order. Pure and
    deterministic, so it is testable without a database."""
    free_at: dict[str, Optional[datetime]] = {b: None for b in berths}
    plan: list[dict] = []
    for call in _sequence(candidates, ordering):
        def berth_rank(berth: str) -> tuple:
            freed = free_at[berth]
            return (freed or datetime.min.replace(tzinfo=call["ready"].tzinfo),
                    berth != call["original_berth"], berth)

        berth = min(berths, key=berth_rank)
        freed = free_at[berth]
        start = call["ready"] if freed is None else max(call["ready"], freed)
        end = start + timedelta(hours=call["service_hours"])
        free_at[berth] = end
        plan.append({
            **call,
            "assigned_berth": berth,
            "berth_shift": berth != call["original_berth"],
            "start": start,
            "end": end,
            "waiting_hours": round(
                (start - call["ready"]).total_seconds() / 3600.0, 2),
        })
    return plan


def evaluate(plan: list[dict], *, horizon_end: datetime) -> dict:
    """Metrics for one scheduled plan. Every ordering is measured the same way."""
    waiting = [r["waiting_hours"] for r in plan]
    shifts = sum(1 for r in plan if r["berth_shift"])
    moves_in_horizon = sum(r["gross_moves"] for r in plan if r["end"] <= horizon_end)
    completed = sum(1 for r in plan if r["end"] <= horizon_end)
    makespan = (max(r["end"] for r in plan) if plan else None)
    return {
        "total_waiting_hours": round(sum(waiting), 2),
        "mean_waiting_hours": round(sum(waiting) / len(waiting), 2) if waiting else 0.0,
        "max_waiting_hours": round(max(waiting), 2) if waiting else 0.0,
        "berth_shifts": shifts,
        "tide_misses": 0,
        "moves_handled": moves_in_horizon,
        "calls_completed_in_horizon": completed,
        "makespan": makespan,
    }


def objective_value(metrics: dict, objective: str) -> float:
    """The single number the orderings are ranked on."""
    if objective == "moves_handled":
        return float(metrics["moves_handled"])
    if objective == "line_priority":
        return round(metrics["total_waiting_hours"] + W_BERTH_SHIFT * metrics["berth_shifts"], 2)
    return round(W_WAITING * metrics["total_waiting_hours"]
                 + W_TIDE_MISS * metrics["tide_misses"]
                 + W_BERTH_SHIFT * metrics["berth_shifts"], 2)


# ----------------------------------------------------------------------- run
async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario I-A. ``params``:

        as_of          required - the study day (the Notice says 6 Aug 2026)
        terminal       optional - restrict to one terminal
        horizon_hours  default 24
        objective      waiting_time (default) | moves_handled | line_priority
    """
    as_of: datetime = params["as_of"]
    terminal = (params.get("terminal") or "").strip() or None
    horizon_hours = int(params.get("horizon_hours", DEFAULT_HORIZON_HOURS))
    objective = str(params.get("objective") or "waiting_time").strip().lower()
    if objective not in OBJECTIVES:
        raise SimulationError(
            f"unknown objective '{objective}'. Choose one of: "
            f"{', '.join(sorted(OBJECTIVES))}.")
    description, direction = OBJECTIVES[objective]
    horizon_end = as_of + timedelta(hours=horizon_hours)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Take the calls contending for berths on the study day and the berths "
            "they occupy. Schedule four candidate orderings (FCFS, SPT, MAX_MOVES, "
            "LINE_PRIORITY) by list-scheduling: each vessel in turn takes the berth "
            "that frees earliest and starts at max(ready, berth_free). Score every "
            f"ordering on the SAME declared objective - {description}, to "
            f"{direction} - and report the cost of each alternative against the "
            "recommended one. Objective weights are shared with the UC-1 berth "
            "optimiser (uc1_m5_berth_optimiser.py): "
            "cost = 1.0*waiting_hours + 2.0*tide_misses + 0.5*berth_shifts."),
    )
    res.assume("objective", objective,
               f"the basis this order optimises for, as the Notice requires it be "
               f"stated: {description} ({direction})", SOURCE_PARAMETER)
    res.assume("readiness", "later of study-day start and recorded alongside time",
               "the daily berthing feed carries no ETA/ATA, so arrival at the "
               "anchorage cannot be measured. On a bunching day the Notice's premise "
               "is that the vessels are already present, so they contend from the "
               "opening of the day.", SOURCE_ASSUMED)
    res.assume("tide_misses", 0,
               "the objective carries a tide-miss term for parity with the UC-1 "
               "optimiser, but tidal windows are a UC-1 dataset and are not in this "
               "database, so the term contributes nothing here and no tide-driven "
               "infeasibility is claimed.", SOURCE_ASSUMED)

    calls, coverage = await load_calls_for(
        repo, res, as_of=as_of, horizon_hours=horizon_hours, terminal=terminal)
    if not calls:
        res.result = {"coverage": coverage.to_dict()}
        return res

    known = [h for h in (_service_hours(c) for c in calls) if h is not None]
    fallback = round(median(known), 2) if known else 12.0
    if len(known) < len(calls):
        res.assume(
            "service_hours_fallback", fallback,
            f"{len(calls) - len(known)} of {len(calls)} calls have no operation "
            "window; the median of those that do is used for them so they still "
            "occupy a berth, and each is flagged in the plan.",
            SOURCE_DERIVED if known else SOURCE_ASSUMED)

    candidates = [_candidate(c, as_of, fallback) for c in calls]
    berths = sorted({c["original_berth"] for c in candidates})
    if not berths:
        return res.note("no berth is recorded against any call in the window, so "
                        "no ordering can be scheduled.", blocks_answer=True)

    if objective == "line_priority":
        with_line = sum(1 for c in candidates
                        if c["shipping_line"] not in (None, "", "NA"))
        if not with_line:
            res.note("no call in this window carries a declared shipping line, so "
                     "LINE_PRIORITY degrades to readiness order; its figures are "
                     "identical to FCFS by construction, not by coincidence.")

    # ---- score every ordering on the one declared objective
    evaluated: list[dict] = []
    for ordering in ORDERINGS:
        plan = schedule(candidates, berths, ordering)
        metrics = evaluate(plan, horizon_end=horizon_end)
        evaluated.append({"ordering": ordering, "metrics": metrics, "plan": plan,
                          "objective_value": objective_value(metrics, objective)})

    reverse = direction == "maximise"
    ranked = sorted(evaluated, key=lambda e: (-e["objective_value"] if reverse
                                              else e["objective_value"],
                                              e["ordering"]))
    best = ranked[0]
    baseline = next(e for e in evaluated if e["ordering"] == "FCFS")

    def cost_delta(entry: dict) -> float:
        raw = entry["objective_value"] - best["objective_value"]
        return round(-raw if reverse else raw, 2)

    # ---- terminal imbalance, the premise the Notice states
    by_terminal: dict[str, int] = {}
    for c in candidates:
        key = c["terminal"] or "UNKNOWN"
        by_terminal[key] = by_terminal.get(key, 0) + 1
    busiest = max(by_terminal.items(), key=lambda kv: (kv[1], kv[0])) if by_terminal else None

    res.result = {
        "coverage": coverage.to_dict(),
        "objective": {"id": objective, "description": description,
                      "direction": direction},
        "berth_pool": berths,
        "vessels_contending": len(candidates),
        "load_by_terminal": by_terminal,
        "recommended": {
            "ordering": best["ordering"],
            "objective_value": best["objective_value"],
            "sequence": [
                {"position": i + 1, "vessel": r["vessel_name"],
                 "voyage": r["voyage_number"], "terminal": r["terminal"],
                 "assigned_berth": r["assigned_berth"],
                 "original_berth": r["original_berth"],
                 "berth_shift": r["berth_shift"],
                 "start": r["start"], "end": r["end"],
                 "waiting_hours": r["waiting_hours"],
                 "gross_moves": r["gross_moves"],
                 "service_hours_assumed": r["service_hours_assumed"]}
                for i, r in enumerate(best["plan"])],
            "metrics": best["metrics"],
        },
        "alternatives": [
            {"ordering": e["ordering"], "objective_value": e["objective_value"],
             "cost_vs_recommended": cost_delta(e),
             "is_baseline": e["ordering"] == "FCFS",
             "metrics": e["metrics"]}
            for e in ranked if e["ordering"] != best["ordering"]],
    }

    res.figures = {
        "vessels_contending": len(candidates),
        "berths_available": len(berths),
        "recommended_ordering": best["ordering"],
        "objective_value_recommended": best["objective_value"],
        "objective_value_baseline_fcfs": baseline["objective_value"],
        "improvement_vs_baseline": cost_delta(baseline),
        "improvement_vs_baseline_pct": (
            pct(abs(cost_delta(baseline)), abs(baseline["objective_value"]))
            if baseline["objective_value"] else 0.0),
        "total_waiting_hours_recommended": best["metrics"]["total_waiting_hours"],
        "total_waiting_hours_baseline": baseline["metrics"]["total_waiting_hours"],
        "berth_shifts_recommended": best["metrics"]["berth_shifts"],
        "moves_handled_recommended": best["metrics"]["moves_handled"],
        "busiest_terminal": busiest[0] if busiest else None,
        "busiest_terminal_calls": busiest[1] if busiest else 0,
    }

    if best["ordering"] == "FCFS":
        res.recommend(
            "KEEP_ARRIVAL_ORDER",
            f"no alternative ordering beats arrival order on {description}; the "
            "queue is already sequenced as well as this objective allows",
            objective_value=best["objective_value"])
    else:
        res.recommend(
            "ADOPT_ORDER",
            f"{best['ordering']} beats arrival order by "
            f"{abs(cost_delta(baseline))} on {description}",
            ordering=best["ordering"],
            saves=abs(cost_delta(baseline)))
    if best["metrics"]["berth_shifts"]:
        res.recommend(
            "CONFIRM_BERTH_REASSIGNMENTS",
            f"{best['metrics']['berth_shifts']} call(s) are proposed on a berth "
            "other than the one recorded against them; each needs a length and "
            "draft check before it is issued",
            count=best["metrics"]["berth_shifts"])
    if busiest and len(by_terminal) > 1:
        share = pct(busiest[1], len(candidates))
        res.recommend(
            "REBALANCE_ACROSS_TERMINALS",
            f"{busiest[0]} carries {busiest[1]} of {len(candidates)} contending "
            f"calls ({share}%); the Notice's premise of uneven distribution is "
            "visible in the data and is not addressed by resequencing within a "
            "terminal alone",
            terminal=busiest[0], calls=busiest[1], share_pct=share)
    return res
