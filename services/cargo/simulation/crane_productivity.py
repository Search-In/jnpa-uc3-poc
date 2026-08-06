"""Scenario II-B — Equipment Availability.

    "Derive the effective crane productivity implied by the data for each vessel
     call, expressed as gross moves per hour worked. Model a twenty-five per cent
     reduction in that productivity for one call and state the effect on
     turnaround and on the berth queue behind it. Take up a vessel on 6th August
     2026."                                      — JNPA Notice, 05 Aug 2026, §3 II-B

Method
------
Baseline, per call::

    productivity (moves/hour) = gross_moves / hours_worked
    hours_worked              = cargo_operation_end - cargo_operation_start

``gross_moves`` comes from ``core.vessel_call_moves`` (migration 0129).
``hours_worked`` from ``core.berthing_record``, which already carried it.

Reduction::

    reduced_rate  = baseline_rate * (1 - reduction_pct)
    new_hours     = gross_moves / reduced_rate
                  = hours_worked / (1 - reduction_pct)
    delta_hours   = new_hours - hours_worked

The identity in the second line is worth stating because it is what makes the
answer robust: the turnaround extension depends only on the reduction fraction
and the hours worked, so it holds even where the move count is a DERIVED proxy.
The move count still matters for the productivity FIGURE itself, and every call
whose count is derived says so.

The berth-queue effect is then exactly scenario I-B's question, so it is answered
by the same code: :func:`services.cargo.simulation.berth_cascade.cascade` with
``delta_hours``.

Honesty rules
-------------
* A call with no move count gets ``productivity: null`` and is listed under
  ``calls_without_moves`` — never a substituted average.
* A call with no operation window gets ``hours_worked: null`` for the same reason.
* ``data_origin='DERIVED'`` on a move count is surfaced per call AND as a
  scenario-level assumption, because a manifest line count excludes restows.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .base import (SOURCE_DERIVED, SOURCE_MEASURED, SOURCE_PARAMETER,
                   SimulationResult, hours_between, pct)
from .berth_cascade import DEFAULT_HORIZON_HOURS, cascade

SCENARIO = "crane-productivity"
DEFAULT_REDUCTION = 0.25


def _rate(gross_moves: Optional[int], hours: Optional[float]) -> Optional[float]:
    """Gross moves per hour worked, or None when either side is unknown.

    None rather than 0: a call with no reported hours has an UNKNOWN rate, and a
    zero denominator would otherwise produce an infinite productivity."""
    if not gross_moves or not hours:
        return None
    return round(gross_moves / hours, 2)


def _call_row(row: dict) -> dict:
    start = row.get("cargo_operation_start") or row.get("berthing_time")
    end = row.get("cargo_operation_end") or row.get("departure_time")
    hours = hours_between(start, end)
    moves = row.get("gross_moves")
    cranes = row.get("cranes_deployed")
    rate = _rate(moves, hours)
    return {
        "berthing_record_id": row.get("berthing_record_id"),
        "terminal": row.get("terminal"),
        "vessel_name": row.get("vessel_name"),
        "voyage_number": row.get("voyage_number"),
        "berth_number": row.get("berth_number"),
        "operation_start": start,
        "operation_end": end,
        "hours_worked": hours,
        "gross_moves": moves,
        "discharge_moves": row.get("discharge_moves"),
        "load_moves": row.get("load_moves"),
        "cranes_deployed": cranes,
        "moves_per_hour": rate,
        "moves_per_crane_hour": (round(rate / cranes, 2)
                                 if rate is not None and cranes else None),
        "moves_data_origin": row.get("data_origin"),
        "moves_source_note": row.get("source_note"),
        "derivable": rate is not None,
    }


async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario II-B. ``params``:

        terminal           optional — restrict to one terminal
        as_of              required — ISO timestamp; the day under study
        window_hours       default 48 (baseline table + cascade horizon)
        reduction_pct      default 0.25, per the Notice
        vessel_name        } the call to slow down; when none is given the call
        voyage_number      } with the HIGHEST derivable productivity is chosen
        berthing_record_id } (the biggest loser from a cut) and that is declared
    """
    terminal = (params.get("terminal") or "").strip() or None
    as_of: datetime = params["as_of"]
    window_hours = int(params.get("window_hours", DEFAULT_HORIZON_HOURS))
    reduction = float(params.get("reduction_pct", DEFAULT_REDUCTION))
    to_ts = as_of + timedelta(hours=window_hours)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Per vessel call: productivity = gross_moves / hours_worked, where "
            "gross_moves is core.vessel_call_moves (migration 0129) and "
            "hours_worked = cargo_operation_end - cargo_operation_start from "
            f"core.berthing_record. Apply a {pct(reduction, 1.0, digits=0)}% cut: "
            "reduced_rate = rate x (1 - r), so new_hours = hours_worked / (1 - r) "
            "and delta = new_hours - hours_worked. Feed delta into the per-berth "
            "cascade (scenario I-B) to get the queue effect behind the call."),
    )
    res.assume("reduction_pct", reduction,
               "the productivity cut stated in the scenario", SOURCE_PARAMETER)
    res.assume("hours_worked", "cargo_operation_end - cargo_operation_start",
               "elapsed operation window is used as hours worked; the data does "
               "not distinguish worked hours from idle time inside the window, so "
               "this is elapsed time, not gang-hours",
               SOURCE_MEASURED)
    res.assume("moves_unchanged_under_reduction", True,
               "a productivity cut changes the RATE, not the workload: the same "
               "gross moves are handled over a longer window", SOURCE_PARAMETER)

    rows, trace = await repo.calls_with_moves(terminal=terminal, from_ts=as_of,
                                              to_ts=to_ts)
    res.trace(trace)
    if not rows:
        return res.note(
            f"core.berthing_record returned no calls for {terminal or 'any terminal'} "
            f"between {as_of.isoformat()} and {to_ts.isoformat()} — productivity "
            "cannot be derived and no figure is invented.", blocks_answer=True)

    calls = [_call_row(r) for r in rows]
    derivable = [c for c in calls if c["derivable"]]
    no_moves = [c for c in calls if c["gross_moves"] in (None, 0)]
    no_hours = [c for c in calls if c["hours_worked"] is None]

    if not derivable:
        res.note(
            f"{len(calls)} calls found, but none has both a move count and an "
            "operation window: "
            f"{len(no_moves)} lack core.vessel_call_moves rows, "
            f"{len(no_hours)} lack cargo_operation_start/end. Productivity is not "
            "derivable for this window — reporting that rather than a substituted "
            "fleet average.", blocks_answer=True)
        res.result = {"calls": calls, "calls_without_moves":
                      [c["vessel_name"] for c in no_moves]}
        return res

    derived_origin = [c for c in derivable if c["moves_data_origin"] == "DERIVED"]
    if derived_origin:
        res.assume(
            "gross_moves", "DERIVED from core.edi_vessel_container",
            f"{len(derived_origin)} of {len(derivable)} calls have no JNPA-published "
            "move count; the EDI manifest line count per VCN is used as a proxy. It "
            "excludes restows and any box handled outside the manifest, so the "
            "productivity figure for those calls is a lower bound",
            SOURCE_DERIVED)

    # Pick the call to slow down.
    target: Optional[dict] = None
    if params.get("berthing_record_id") is not None:
        target = next((c for c in derivable
                       if c["berthing_record_id"] == params["berthing_record_id"]), None)
    elif params.get("vessel_name"):
        want = str(params["vessel_name"]).strip().upper()
        voy = str(params.get("voyage_number") or "").strip().upper()
        target = next((c for c in derivable
                       if str(c["vessel_name"] or "").strip().upper() == want
                       and (not voy or str(c["voyage_number"] or "").strip().upper() == voy)),
                      None)
    if target is None and (params.get("vessel_name") or params.get("berthing_record_id")):
        return res.note(
            "the requested vessel has no derivable productivity in this window "
            "(missing move count or operation window).", blocks_answer=True)
    if target is None:
        target = max(derivable, key=lambda c: (c["moves_per_hour"], c["vessel_name"] or ""))
        res.assume("target_call", target["vessel_name"],
                   "no vessel was named, so the call with the highest derivable "
                   "productivity is modelled — the one a 25% cut costs most",
                   SOURCE_DERIVED)

    # ---- the arithmetic
    base_rate = target["moves_per_hour"]
    base_hours = target["hours_worked"]
    reduced_rate = round(base_rate * (1.0 - reduction), 2)
    new_hours = round(base_hours / (1.0 - reduction), 2) if reduction < 1 else None
    delta_hours = round(new_hours - base_hours, 2) if new_hours else None

    if delta_hours is None:
        return res.note("a 100% productivity reduction has no finite turnaround.",
                        blocks_answer=True)

    # ---- queue effect: same cascade as scenario I-B
    target_index = next(i for i, r in enumerate(rows)
                        if r.get("berthing_record_id") == target["berthing_record_id"])
    plan = cascade(rows_as_calls(rows), target_index=target_index,
                   delta_hours=delta_hours,
                   default_duration_hours=_median_duration(calls))
    displaced = [r for r in plan if r["delay_hours"] > 0 and not r["is_target"]]
    cumulative = round(sum(r["delay_hours"] for r in displaced), 2)

    fleet_rates = [c["moves_per_hour"] for c in derivable]
    res.result = {
        "terminal": terminal,
        "window": {"from": as_of, "to": to_ts, "hours": window_hours},
        "baseline_by_call": calls,
        "calls_without_moves": [c["vessel_name"] for c in no_moves],
        "target_call": {
            "vessel_name": target["vessel_name"],
            "voyage_number": target["voyage_number"],
            "berth_number": target["berth_number"],
            "gross_moves": target["gross_moves"],
            "cranes_deployed": target["cranes_deployed"],
            "moves_data_origin": target["moves_data_origin"],
        },
        "before": {"moves_per_hour": base_rate, "hours_worked": base_hours,
                   "operation_end": target["operation_end"]},
        "after": {"moves_per_hour": reduced_rate, "hours_worked": new_hours,
                  "operation_end": (target["operation_end"]
                                    + timedelta(hours=delta_hours)
                                    if target["operation_end"] else None)},
        "berth_queue_impact": [
            {"vessel": r["vessel_name"], "voyage": r["voyage_number"],
             "berth": r["berth_number"], "original_time": r["original_start"],
             "new_time": r["new_start"], "delay_hours": r["delay_hours"]}
            for r in displaced],
    }
    res.figures = {
        "calls_in_window": len(calls),
        "calls_with_derivable_productivity": len(derivable),
        "fleet_mean_moves_per_hour": round(sum(fleet_rates) / len(fleet_rates), 2),
        "fleet_min_moves_per_hour": min(fleet_rates),
        "fleet_max_moves_per_hour": max(fleet_rates),
        "baseline_moves_per_hour": base_rate,
        "reduced_moves_per_hour": reduced_rate,
        "baseline_turnaround_hours": base_hours,
        "reduced_turnaround_hours": new_hours,
        "turnaround_increase_hours": delta_hours,
        "turnaround_increase_pct": pct(delta_hours, base_hours),
        "calls_displaced": len(displaced),
        "cumulative_berth_delay_hours": cumulative,
        "total_delay_hours": round(delta_hours + cumulative, 2),
    }

    res.recommend(
        "ADD_CRANE_CAPACITY",
        f"restoring {pct(reduction, 1.0, digits=0)}% of the rate on "
        f"{target['vessel_name']} recovers {delta_hours}h of its own turnaround "
        f"and {cumulative}h across the {len(displaced)} calls behind it",
        vessel=target["vessel_name"], recovers_hours=round(delta_hours + cumulative, 2))
    if displaced:
        res.recommend(
            "RESEQUENCE_BERTH",
            "the queue behind the slowed call inherits the whole overrun; moving "
            "the next call to a berth free in the window caps the propagation",
            berth=target["berth_number"],
            first_displaced=displaced[0]["vessel_name"])
    if target["cranes_deployed"] is None:
        res.note("cranes_deployed is not reported for this call, so the figure is "
                 "moves per VESSEL hour, not per crane hour.")
    return res


def rows_as_calls(rows: list[dict]) -> list[dict]:
    """Adapt the moves-join rows to the shape :func:`cascade` expects (it keys on
    ``id``; this query aliases the berthing PK as ``berthing_record_id``)."""
    out = []
    for r in rows:
        c = dict(r)
        c["id"] = r.get("berthing_record_id")
        out.append(c)
    return out


def _median_duration(calls: list[dict]) -> Optional[float]:
    known = sorted(c["hours_worked"] for c in calls if c["hours_worked"] is not None)
    if not known:
        return None
    mid = len(known) // 2
    return round(known[mid] if len(known) % 2 else (known[mid - 1] + known[mid]) / 2, 2)
