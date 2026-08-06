"""Scenario III-A — Gate Approach Congestion.

    "Using vehicle arrival times at the gate, characterise the arrival pattern
     across the day and identify the periods in which arrivals exceed the rate the
     gate sustains. Propose an appointment or slotting arrangement that flattens
     the peak and quantify what it would achieve against the observed pattern."
                                                — JNPA Notice, 05 Aug 2026, §4 III-A

Method
------
1. Bucket real gate arrivals by hour over the requested window
   (``core.eir.truck_in_time``; ``core.gate_event`` as fallback).
2. Establish the rate the gate SUSTAINS. In preference order, so the strongest
   available evidence wins and the choice is always declared:
       a. an explicit ``sustained_rate`` in the request       (PARAMETER)
       b. declared TAS slot capacity for the window           (MEASURED)
       c. the 90th percentile of observed hourly COMPLETIONS  (DERIVED)
       d. the 90th percentile of observed hourly arrivals     (DERIVED, weakest)
   (d) is weakest because arrivals are demand, not throughput — it is used only
   when nothing records what the gate actually cleared, and it says so.
3. Flag every hour where arrivals exceed that rate, and total the excess.
4. Propose slotting: cap each hour at the sustained rate and spill the excess
   forward into the next hours with headroom. Report the new peak, the peak
   reduction, and anything that still does not fit inside the window.

This module also exports :func:`load_profile` and :func:`derive_sustained_rate`,
which scenario II-A (:mod:`.modal_shift`) reuses — the two scenarios must agree
on what "the rate the gate sustains" means or their answers contradict.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .base import (SOURCE_DERIVED, SOURCE_MEASURED, SOURCE_PARAMETER,
                   Assumption, QueryTrace, SimulationResult, pct)

SCENARIO = "gate-slotting"
#: Which percentile of the observed distribution is treated as "sustained".
#: p90 rather than the max: the single busiest hour of a window is an outlier, not
#: a rate the gate holds. Declared in every response that uses it.
SUSTAINED_PERCENTILE = 0.90


def percentile(values: list[float], q: float) -> Optional[float]:
    """Nearest-rank percentile. Deterministic and dependency-free (no numpy in the
    gateway image); ``None`` for an empty series.

    Uses conventional half-up rounding (``floor(x + 0.5)``) rather than Python's
    ``round``, which is banker's rounding: ``round(4.5) == 4`` would make the p90
    of a six-hour window silently pick the fifth value instead of the sixth. The
    figure ends up in a JNPA answer, so the rule has to be the obvious one."""
    if not values:
        return None
    ordered = sorted(values)
    rank = int(q * (len(ordered) - 1) + 0.5)
    idx = max(0, min(len(ordered) - 1, rank))
    return float(ordered[idx])


async def load_profile(repo: Any, *, from_ts: datetime, to_ts: datetime,
                       terminal: Optional[str] = None,
                       gate_id: Optional[str] = None
                       ) -> tuple[list[dict], list[QueryTrace], list[Assumption], str]:
    """Hourly arrival profile for the window, with the source it came from.

    Returns ``(profile, traces, assumptions, source)`` where each profile row is
    ``{bucket, arrivals, completed, unique_trucks}``. ``core.eir`` is preferred —
    it is real JNPA gate paperwork with a truck-in timestamp. ``core.gate_event``
    is the fallback. Hours with no activity are NOT filled in: an absent hour is
    absent, and the caller decides whether zero or unknown is the right reading."""
    traces: list[QueryTrace] = []
    assumptions: list[Assumption] = []

    rows, trace = await repo.gate_hourly_profile(from_ts=from_ts, to_ts=to_ts,
                                                 terminal=terminal)
    traces.append(trace)
    if rows:
        profile = [{"bucket": r["bucket"], "arrivals": int(r["arrivals"] or 0),
                    "completed": int(r.get("completed") or 0),
                    "unique_trucks": int(r.get("unique_trucks") or 0),
                    "avg_tat_min": r.get("avg_tat_min")}
                   for r in rows]
        return profile, traces, assumptions, "core.eir"

    rows, trace = await repo.gate_event_hourly(from_ts=from_ts, to_ts=to_ts,
                                               gate_id=gate_id)
    traces.append(trace)
    if rows:
        assumptions.append(Assumption(
            "arrival_source", "core.gate_event",
            "core.eir has no gate documents in this window, so telemetry gate "
            "events are used instead; GATE_ARRIVAL is the arrival signal and "
            "GATE_IN the completion",
            SOURCE_DERIVED))
        profile = [{"bucket": r["bucket"],
                    # A window with no GATE_ARRIVAL rows but with GATE_IN rows
                    # still describes demand — fall back rather than report zero.
                    "arrivals": int(r["arrivals"] or 0) or int(r.get("gate_in") or 0),
                    "completed": int(r.get("gate_in") or 0),
                    "unique_trucks": int(r.get("unique_trucks") or 0),
                    "avg_tat_min": None}
                   for r in rows]
        return profile, traces, assumptions, "core.gate_event"

    return [], traces, assumptions, "NONE"


async def derive_sustained_rate(repo: Any, profile: list[dict], *,
                                from_ts: datetime, to_ts: datetime,
                                gate_id: Optional[str] = None,
                                override: Optional[float] = None
                                ) -> tuple[Optional[float], Optional[QueryTrace],
                                           Assumption]:
    """The rate the gate sustains, per hour, plus the assumption declaring it.

    See the module docstring for the preference order. Returns
    ``(rate, trace_or_None, assumption)``; ``rate`` is ``None`` only when the
    profile is empty and no capacity is declared anywhere."""
    if override:
        return float(override), None, Assumption(
            "gate_sustained_rate", float(override),
            "supplied in the request; the observed data was not used to infer it",
            SOURCE_PARAMETER)

    rows, trace = await repo.tas_hourly_capacity(from_ts=from_ts, to_ts=to_ts,
                                                 gate_id=gate_id)
    caps = [float(r["slot_capacity"]) for r in rows if r.get("slot_capacity")]
    if caps:
        rate = round(sum(caps) / len(caps), 1)
        return rate, trace, Assumption(
            "gate_sustained_rate", rate,
            f"mean declared TAS slot capacity across {len(caps)} provisioned hours "
            "(core.tas_appointment) — a policy figure, not an inference",
            SOURCE_MEASURED)

    completions = [float(h["completed"]) for h in profile if h.get("completed")]
    if completions:
        rate = round(percentile(completions, SUSTAINED_PERCENTILE) or 0.0, 1)
        return rate, trace, Assumption(
            "gate_sustained_rate", rate,
            f"p{int(SUSTAINED_PERCENTILE * 100)} of observed hourly gate "
            f"COMPLETIONS across {len(completions)} active hours; the busiest "
            "single hour is treated as an outlier rather than a sustainable rate",
            SOURCE_DERIVED)

    arrivals = [float(h["arrivals"]) for h in profile if h.get("arrivals")]
    if arrivals:
        rate = round(percentile(arrivals, SUSTAINED_PERCENTILE) or 0.0, 1)
        return rate, trace, Assumption(
            "gate_sustained_rate", rate,
            f"p{int(SUSTAINED_PERCENTILE * 100)} of observed hourly ARRIVALS — "
            "nothing in the window records what the gate actually cleared, so "
            "demand is used as a proxy for throughput. This is the weakest of the "
            "available bases and will understate capacity if the gate was never "
            "saturated in this window",
            SOURCE_DERIVED)

    return None, trace, Assumption(
        "gate_sustained_rate", None,
        "no arrivals, no completions and no declared slot capacity in the window",
        SOURCE_DERIVED)


def flatten(profile: list[dict], rate: float) -> dict:
    """Cap each hour at ``rate`` and spill the excess forward into later hours that
    still have headroom. Deterministic greedy pass, earliest hour first.

    Forward-only is deliberate: an appointment system can defer an arrival, it
    cannot summon one earlier than the box is ready. Anything still unplaced at
    the end of the window is reported as ``unplaced`` rather than quietly dropped."""
    hours = [{"bucket": h["bucket"], "original": int(h["arrivals"]),
              "slotted": min(int(h["arrivals"]), int(rate)),
              "deferred_in": 0, "deferred_out": 0}
             for h in profile]
    carry = 0
    for hour in hours:
        excess = hour["original"] - hour["slotted"]
        hour["deferred_out"] = excess
        carry += excess
        headroom = int(rate) - hour["slotted"]
        if carry and headroom > 0:
            take = min(carry, headroom)
            hour["slotted"] += take
            hour["deferred_in"] = take
            carry -= take
    return {"hours": hours, "unplaced": carry}


async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario III-A. ``params``: ``from_ts``, ``to_ts`` (required),
    ``terminal``, ``gate_id``, ``sustained_rate`` (optional override)."""
    from_ts: datetime = params["from_ts"]
    to_ts: datetime = params["to_ts"]
    terminal = (params.get("terminal") or "").strip() or None
    gate_id = (params.get("gate_id") or "").strip() or None

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Bucket real gate arrivals by hour from core.eir.truck_in_time "
            "(core.gate_event as fallback) over the requested window. Establish "
            "the sustained gate rate from declared TAS slot capacity if "
            f"provisioned, else the p{int(SUSTAINED_PERCENTILE * 100)} of observed "
            "hourly completions. Flag every hour whose arrivals exceed it. Then "
            "cap each hour at that rate and spill the excess forward into later "
            "hours with headroom, and compare the resulting peak with the "
            "observed one."),
    )

    profile, traces, prof_assumptions, source = await load_profile(
        repo, from_ts=from_ts, to_ts=to_ts, terminal=terminal, gate_id=gate_id)
    res.trace_all(traces)
    for a in prof_assumptions:
        res.assumptions.append(a)

    if not profile:
        return res.note(
            f"no gate arrivals recorded between {from_ts.isoformat()} and "
            f"{to_ts.isoformat()} in core.eir or core.gate_event — the arrival "
            "pattern cannot be characterised and none is invented.",
            blocks_answer=True)

    rate, cap_trace, rate_assumption = await derive_sustained_rate(
        repo, profile, from_ts=from_ts, to_ts=to_ts, gate_id=gate_id,
        override=params.get("sustained_rate"))
    res.trace(cap_trace)
    res.assumptions.append(rate_assumption)
    res.assume("arrival_source", source,
               "the table the hourly arrival pattern was read from", SOURCE_MEASURED)

    arrivals = [h["arrivals"] for h in profile]
    total = sum(arrivals)
    active_hours = len(profile)
    mean = round(total / active_hours, 2) if active_hours else 0.0
    peak_hour = max(profile, key=lambda h: (h["arrivals"], h["bucket"]))

    if not rate:
        res.result = {"observed_profile": profile}
        res.figures = {"total_arrivals": total, "active_hours": active_hours,
                       "mean_arrivals_per_hour": mean,
                       "peak_arrivals": peak_hour["arrivals"]}
        return res.note("no sustainable rate could be established, so saturated "
                        "hours cannot be identified.", blocks_answer=True)

    over = [h for h in profile if h["arrivals"] > rate]
    excess_total = round(sum(h["arrivals"] - rate for h in over), 1)

    plan = flatten(profile, rate)
    new_peak = max((h["slotted"] for h in plan["hours"]), default=0)

    res.result = {
        "window": {"from": from_ts, "to": to_ts},
        "terminal": terminal,
        "gate_id": gate_id,
        "arrival_pattern": {
            "hourly": profile,
            "peak_hour": peak_hour["bucket"],
            "peak_arrivals": peak_hour["arrivals"],
            "peak_to_mean_ratio": round(peak_hour["arrivals"] / mean, 2) if mean else None,
            "shape": ("PEAKED" if mean and peak_hour["arrivals"] > 1.5 * mean
                      else "FLAT"),
        },
        "saturated_periods": [
            {"hour": h["bucket"], "arrivals": h["arrivals"],
             "sustained_rate": rate,
             "excess": round(h["arrivals"] - rate, 1)}
            for h in over],
        "proposed_slots": [
            {"hour": h["bucket"], "cap": int(rate), "booked": h["slotted"],
             "deferred_out": h["deferred_out"], "absorbed_from_earlier": h["deferred_in"]}
            for h in plan["hours"]],
    }
    res.figures = {
        "total_arrivals": total,
        "active_hours": active_hours,
        "mean_arrivals_per_hour": mean,
        "sustained_rate_per_hour": rate,
        "observed_peak": peak_hour["arrivals"],
        "saturated_hours": len(over),
        "excess_arrivals": excess_total,
        "excess_share_pct": pct(excess_total, total),
        "slotted_peak": new_peak,
        "peak_reduction": round(peak_hour["arrivals"] - new_peak, 1),
        "peak_reduction_pct": pct(peak_hour["arrivals"] - new_peak,
                                  peak_hour["arrivals"]),
        "arrivals_not_placeable_in_window": plan["unplaced"],
    }

    if over:
        res.recommend(
            "APPOINTMENT_CAP",
            f"cap gate bookings at {int(rate)} trucks/hour; that removes "
            f"{res.figures['peak_reduction']} trucks from the "
            f"{peak_hour['arrivals']}-truck peak "
            f"({res.figures['peak_reduction_pct']}% lower)",
            cap_per_hour=int(rate), saturated_hours=len(over))
        res.recommend(
            "DEFER_TO_HEADROOM",
            f"{int(excess_total)} arrivals across {len(over)} saturated hours can "
            "be re-timed into hours that already run below the sustained rate",
            excess=int(excess_total))
    else:
        res.recommend("NO_ACTION",
                      f"no hour in the window exceeds {rate} arrivals — the gate "
                      "absorbs the observed pattern without slotting")
    if plan["unplaced"]:
        res.note(f"{plan['unplaced']} arrivals cannot be placed inside the window "
                 "at the sustained rate; they spill past its end and would need "
                 "either a longer horizon or added capacity.")
    return res
