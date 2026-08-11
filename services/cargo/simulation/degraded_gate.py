"""Scenario N-3 — Degraded-Mode Gate Outage (bidder-proposed, not JNPA-requested).

    "The gate's automated identification and the terminal system are unavailable
     for N hours. The gate reverts to manual verification at a fraction of its
     normal service rate. How far does the queue back up, and how long does it
     take to clear once systems return?"

Why this scenario
-----------------
Every scenario in the JNPA briefing and the 05 Aug Notice is a **physical**
disruption — weather, tide, labour, equipment, traffic. None is a **digital**
one. Yet the briefing's own evaluation grid scores *07 Cybersecurity* and
*09 Failover & Exceptions* ("edge system offline"), and nothing else in the
catalogue exercises either. This is the scenario that does.

It is also the only one that asks about **recovery** rather than impact: the
question is not just how bad the queue gets, but how long the port takes to
return to normal after the cause is removed. That is the number an operations
centre actually needs during an incident.

Method
------
1. Load the observed hourly arrival profile for the window and derive the rate
   the gate sustains — the *same* :func:`~.gate_slotting.derive_sustained_rate`
   that III-A and II-A use, so all three agree on gate capacity by construction.
2. During the outage window, service drops to ``degraded_fraction x rate``.
   Arrivals are unchanged: an outage does not stop trucks turning up, which is
   the whole problem.
3. Walk the window hour by hour maintaining a queue::

       served(h)  = min(arrivals(h) + queue(h-1), capacity(h))
       queue(h)   = queue(h-1) + arrivals(h) - served(h)

   where ``capacity(h)`` is the degraded rate inside the outage and the normal
   rate outside it.
4. Recovery is the first hour after the outage ends at which the queue returns
   to its pre-outage level.

Honesty rules
-------------
* ``degraded_fraction`` is a declared input, not a measurement. Nothing in the
  data records how fast a manual gate runs, and the response says so.
* Arrivals are held at their observed values. A real outage would eventually
  suppress arrivals through word of mouth and diversion; modelling that would
  need behavioural assumptions the data cannot support, so the figure is an
  upper bound on the queue and is labelled as one.
* If the queue never clears inside the window, that is reported as
  ``recovery_hours: null`` with a note — not as the window length.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_PARAMETER, SimulationResult, pct)
from .gate_slotting import derive_sustained_rate, load_profile

SCENARIO = "degraded-gate"

#: Manual verification as a fraction of the automated rate. A gate clerk checking
#: paperwork by hand against a driver's documents is far slower than an ANPR read
#: plus an automated TOS lookup; 0.4 is a declared planning figure.
DEFAULT_DEGRADED_FRACTION = 0.4
DEFAULT_OUTAGE_HOURS = 4


def simulate_queue(profile: list[dict], *, normal_rate: float,
                   degraded_rate: float, outage_from: datetime,
                   outage_to: datetime) -> list[dict]:
    """Hour-by-hour queue under the outage. Pure and deterministic.

    ``queue`` is the number of trucks still waiting at the end of the hour, so a
    row's ``queue`` is what the next hour inherits."""
    rows: list[dict] = []
    queue = 0.0
    for hour in profile:
        bucket = hour["bucket"]
        arrivals = float(hour.get("arrivals") or 0)
        degraded = outage_from <= bucket < outage_to
        capacity = degraded_rate if degraded else normal_rate
        demand = arrivals + queue
        served = min(demand, capacity)
        queue = round(demand - served, 2)
        rows.append({
            "bucket": bucket,
            "arrivals": int(arrivals),
            "capacity": round(capacity, 1),
            "served": round(served, 1),
            "queue": queue,
            "degraded": degraded,
        })
    return rows


def baseline_queue(profile: list[dict], *, normal_rate: float) -> list[dict]:
    """The same walk with no outage — the comparison arm."""
    far_future = datetime.max.replace(tzinfo=profile[0]["bucket"].tzinfo) \
        if profile else datetime.max
    return simulate_queue(profile, normal_rate=normal_rate,
                          degraded_rate=normal_rate,
                          outage_from=far_future, outage_to=far_future)


def recovery_hour(rows: list[dict], *, outage_to: datetime,
                  target_queue: float) -> Optional[datetime]:
    """First hour at or after the outage ends where the queue is back to normal."""
    for row in rows:
        if row["bucket"] >= outage_to and row["queue"] <= target_queue:
            return row["bucket"]
    return None


async def run(repo: Any, params: dict) -> SimulationResult:
    """``params``: from_ts, to_ts (required); outage_start, outage_hours,
    degraded_fraction, terminal, gate_id, sustained_rate."""
    from_ts: datetime = params["from_ts"]
    to_ts: datetime = params["to_ts"]
    outage_hours = float(params.get("outage_hours", DEFAULT_OUTAGE_HOURS))
    fraction = float(params.get("degraded_fraction", DEFAULT_DEGRADED_FRACTION))
    terminal = (params.get("terminal") or "").strip() or None
    gate_id = (params.get("gate_id") or "").strip() or None

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Take the observed hourly arrival profile and the rate the gate "
            "sustains (the same derivation III-A and II-A use). Inside the outage "
            f"window, service falls to {fraction:.0%} of that rate while arrivals "
            "continue unchanged. Walk the window hour by hour carrying a queue: "
            "served = min(arrivals + queue, capacity); queue += arrivals - served. "
            "Recovery is the first hour after the outage where the queue returns "
            "to its no-outage level."),
    )
    res.assume("degraded_fraction", fraction,
               "manual verification as a fraction of the automated rate. Nothing "
               "in the data records a manual gate's throughput, so this is a "
               "declared planning figure, not a measurement.", SOURCE_PARAMETER)
    res.assume("arrivals_unchanged_during_outage", True,
               "trucks already dispatched keep arriving; the model does not assume "
               "drivers learn of the outage and divert. The queue is therefore an "
               "UPPER bound.", SOURCE_ASSUMED)

    profile, traces, assumptions, source = await load_profile(
        repo, from_ts=from_ts, to_ts=to_ts, terminal=terminal, gate_id=gate_id)
    res.trace_all(traces)
    for assumption in assumptions:
        res.assumptions.append(assumption)
    if not profile:
        return res.note(
            f"no gate arrivals recorded between {from_ts.isoformat()} and "
            f"{to_ts.isoformat()} (source: {source}) — there is no profile to "
            "degrade, so no figure is produced.", blocks_answer=True)

    rate, rate_trace, rate_assumption = await derive_sustained_rate(
        repo, profile, from_ts=from_ts, to_ts=to_ts, gate_id=gate_id,
        override=params.get("sustained_rate"))
    res.trace(rate_trace)
    res.assumptions.append(rate_assumption)
    if not rate:
        return res.note("the rate the gate sustains could not be established, so a "
                        "degraded rate cannot be derived from it.", blocks_answer=True)

    outage_from = params.get("outage_start") or profile[0]["bucket"]
    outage_to = outage_from + timedelta(hours=outage_hours)
    degraded_rate = round(rate * fraction, 1)

    with_outage = simulate_queue(profile, normal_rate=rate,
                                 degraded_rate=degraded_rate,
                                 outage_from=outage_from, outage_to=outage_to)
    without = baseline_queue(profile, normal_rate=rate)

    peak = max(with_outage, key=lambda r: r["queue"])
    peak_baseline = max(r["queue"] for r in without)
    target = next((r["queue"] for r in without if r["bucket"] >= outage_to), 0.0)
    recovered_at = recovery_hour(with_outage, outage_to=outage_to,
                                 target_queue=target)
    recovery_h = (round((recovered_at - outage_to).total_seconds() / 3600.0, 1)
                  if recovered_at else None)
    backlog_at_restore = next(
        (r["queue"] for r in with_outage if r["bucket"] >= outage_to), 0.0)

    res.result = {
        "window": {"from": from_ts, "to": to_ts},
        "outage": {"from": outage_from, "to": outage_to, "hours": outage_hours,
                   "normal_rate": rate, "degraded_rate": degraded_rate},
        "profile_source": source,
        "with_outage": with_outage,
        "without_outage": without,
        "peak_queue": {"bucket": peak["bucket"], "queue": peak["queue"]},
        "recovered_at": recovered_at,
    }
    res.figures = {
        "normal_sustained_rate": rate,
        "degraded_rate": degraded_rate,
        "outage_hours": outage_hours,
        "peak_queue_with_outage": peak["queue"],
        "peak_queue_without_outage": peak_baseline,
        "peak_queue_increase": round(peak["queue"] - peak_baseline, 2),
        "backlog_at_restore": backlog_at_restore,
        "recovery_hours_after_restore": recovery_h,
        "total_incident_hours": (round(outage_hours + recovery_h, 1)
                                 if recovery_h is not None else None),
        "trucks_not_served_during_outage": round(sum(
            r["arrivals"] - r["served"] for r in with_outage if r["degraded"]), 1),
        "throughput_loss_pct": pct(
            sum(r["served"] for r in without) - sum(r["served"] for r in with_outage),
            sum(r["served"] for r in without)),
    }

    if recovery_h is None:
        res.note(
            "the queue had not returned to its no-outage level before the end of "
            "the window, so recovery time is not reported. Extend the window to "
            "bound it — reporting the window length would understate it.")
    res.recommend(
        "PRE_STAGE_MANUAL_FALLBACK",
        f"a {outage_hours:g}h outage leaves {backlog_at_restore:g} trucks queued at "
        f"restore and takes "
        f"{f'{recovery_h}h' if recovery_h is not None else 'longer than this window'}"
        " to clear; staffing extra manual lanes for the outage caps the backlog",
        backlog=backlog_at_restore)
    res.recommend(
        "METER_ARRIVALS_ON_DECLARED_OUTAGE",
        "holding arrivals at source during a planned outage converts an "
        "uncontrolled corridor queue into a managed wait off the approach roads",
        peak_queue=peak["queue"])
    return res
