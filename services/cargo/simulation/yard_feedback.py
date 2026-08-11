"""Scenario N-2 — Yard Saturation Feedback Loop (bidder-proposed).

    "Evacuation drops for N days while discharge continues. Yard occupancy rises.
     Above a threshold, re-handles rise and berth-hour productivity itself
     degrades, which slows sailings, which fills the yard further. Where does it
     settle, and when does it tip?"

Why this scenario
-----------------
All twenty-one what-if obligations in the briefing and the Notice are
**one-directional cascades**: A delays B delays C. This is the only **loop** in
the catalogue, and it runs backwards across the use-case boundary — UC-2 yard
state degrading UC-1 berth productivity, which worsens UC-2 yard state.

That matters because it is how ports actually seize up. A cascade has an end; a
loop has a fixed point, and the operationally useful questions are different:
not "how much delay" but "does this converge or run away", and "which day is the
last one on which intervention is cheap".

The mechanism
-------------
Per day::

    occupancy(d)   = occupancy(d-1) + discharged(d) - evacuated(d)
    utilisation(d) = occupancy(d) / capacity

    # Above the threshold, every additional point of utilisation costs
    # productivity, because boxes are buried deeper and re-handles rise.
    factor(d)      = 1 - slope * max(0, utilisation(d) - threshold) / (1 - threshold)
    discharged(d+1) = nominal_discharge * factor(d)

Two regimes, and which one you are in is the answer
---------------------------------------------------
The feedback is negative — a fuller yard discharges more slowly, which slows the
filling — but that does **not** guarantee it settles. It settles only if the
productivity collapse is deep enough to pull discharge below evacuation::

    nominal_discharge * (1 - slope)   <   nominal_evacuation * (1 - drop)

If that holds, the yard finds a fixed point below capacity: **CONVERGING**. The
port throttles itself to a new equilibrium — which is not good news either, since
the equilibrium is a permanently slower berth, and the cost shows up as vessel
turnaround rather than as a yard alarm.

If it does not hold, the yard fills without bound until it is physically full:
**SATURATING**. From that point a box can only be landed if one leaves, so
discharge is capped at the evacuation rate and everything above it backs up onto
the vessels. That surplus — ``discharge_blocked`` — is the real output of this
scenario: it is the moment the yard stops being a UC-2 problem and becomes a UC-1
one.

Occupancy is therefore capped at capacity. An earlier version of this model let
utilisation run past 100% (it reached 148% in testing), which is not a state a
yard can be in; the blocked volume is tracked explicitly instead.

Honesty rules
-------------
* Yard capacity in TEU is a **declared** input. The corpus carries yard inventory
  but no rated capacity, and there is no per-block occupancy anywhere.
* The occupancy-to-productivity curve is a **declared** relationship. No dataset
  available here measures re-handle rates against utilisation. It is stated as an
  assumption with its shape given explicitly, and the response says plainly that
  the *shape* is assumed even though the *inputs* are measured.
* Because the curve is assumed, the answer is reported as a direction and a
  tipping point, not as a precise TEU figure. Where a number would imply more
  precision than the method carries, it is labelled.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_DERIVED, SOURCE_MEASURED,
                   SOURCE_PARAMETER, SimulationResult, pct)

SCENARIO = "yard-feedback"

DEFAULT_EVACUATION_DROP = 0.5
#: Utilisation above which re-handles begin to bite. Terminal practice puts the
#: comfortable ceiling near 85%; declared, not measured.
DEFAULT_THRESHOLD = 0.85
#: Productivity lost at 100% utilisation relative to the threshold. Declared.
DEFAULT_SLOPE = 0.40
DEFAULT_HORIZON_DAYS = 14


def productivity_factor(utilisation: float, *, threshold: float,
                        slope: float) -> float:
    """Berth productivity multiplier at a given yard utilisation.

    1.0 at or below the threshold; falls linearly to ``1 - slope`` at full. Never
    goes below 0.1 — a yard can be dreadful, but a berth does not stop entirely,
    and letting the factor reach zero would make the loop divide by nothing."""
    if utilisation <= threshold:
        return 1.0
    over = min(1.0, (utilisation - threshold) / max(1e-9, 1.0 - threshold))
    return round(max(0.1, 1.0 - slope * over), 4)


def simulate(*, opening_occupancy: float, capacity: float,
             nominal_discharge: float, nominal_evacuation: float,
             evacuation_drop: float, threshold: float, slope: float,
             days: int) -> list[dict]:
    """Run the loop day by day. Pure and deterministic.

    Returns one row per day with occupancy, utilisation, the productivity factor
    that utilisation implies, and the throughput that factor produced."""
    rows: list[dict] = []
    occupancy = opening_occupancy
    factor = 1.0
    evacuation = nominal_evacuation * (1.0 - evacuation_drop)
    for day in range(1, days + 1):
        wanted = nominal_discharge * factor
        # A box can only be landed if there is ground to put it on. Once the yard
        # is full, discharge is capped by what left today, and the surplus backs
        # up onto the vessels instead of into an impossible occupancy figure.
        headroom = max(0.0, capacity - occupancy) + evacuation
        landed = min(wanted, headroom)
        blocked = round(wanted - landed, 1)
        occupancy = min(capacity, max(0.0, occupancy + landed - evacuation))
        utilisation = occupancy / capacity if capacity else 0.0
        factor = productivity_factor(utilisation, threshold=threshold, slope=slope)
        rows.append({
            "day": day,
            "discharge_wanted": round(wanted, 1),
            "discharged": round(landed, 1),
            "discharge_blocked": blocked,
            "evacuated": round(evacuation, 1),
            "occupancy": round(occupancy, 1),
            "utilisation": round(utilisation, 4),
            "yard_full": utilisation >= 0.9999,
            "productivity_factor": factor,
            "productivity_loss_pct": round((1.0 - factor) * 100, 1),
        })
    return rows


def regime(*, nominal_discharge: float, nominal_evacuation: float,
           evacuation_drop: float, slope: float) -> str:
    """Which regime this parameter set is in, from the closed-form condition.

    CONVERGING when the productivity floor pulls discharge below evacuation, so a
    fixed point below capacity exists. SATURATING otherwise — the yard fills until
    it is physically full and the surplus becomes a vessel-side backlog."""
    floor_discharge = nominal_discharge * (1.0 - slope)
    reduced_evacuation = nominal_evacuation * (1.0 - evacuation_drop)
    return "CONVERGING" if floor_discharge < reduced_evacuation else "SATURATING"


def saturation_day(rows: list[dict]) -> Optional[int]:
    """First day the yard is physically full, or None."""
    for row in rows:
        if row["yard_full"]:
            return row["day"]
    return None


def tipping_day(rows: list[dict], threshold: float) -> Optional[int]:
    """First day utilisation crosses the threshold — the last cheap day to act."""
    for row in rows:
        if row["utilisation"] > threshold:
            return row["day"]
    return None


def converged(rows: list[dict], *, tolerance: float = 0.005) -> Optional[dict]:
    """First day after which utilisation stops moving materially."""
    for i in range(1, len(rows)):
        window = rows[i:]
        if all(abs(r["utilisation"] - window[0]["utilisation"]) <= tolerance
               for r in window):
            return window[0]
    return None


async def run(repo: Any, params: dict) -> SimulationResult:
    """``params``: from_date, to_date (required); evacuation_drop_pct,
    yard_capacity_teu, threshold, slope, horizon_days, terminal."""
    from_date: date = params["from_date"]
    to_date: date = params["to_date"]
    drop = float(params.get("evacuation_drop_pct", DEFAULT_EVACUATION_DROP))
    threshold = float(params.get("threshold", DEFAULT_THRESHOLD))
    slope = float(params.get("slope", DEFAULT_SLOPE))
    horizon = int(params.get("horizon_days", DEFAULT_HORIZON_DAYS))
    terminal = (params.get("terminal") or "").strip() or None

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Measure the daily discharge and evacuation volumes over the observed "
            "window. Cut evacuation by the stated fraction and run the yard "
            "forward day by day: occupancy += discharged - evacuated. Above the "
            "declared utilisation threshold, berth productivity falls linearly "
            "with utilisation (re-handles rise as boxes are buried), and the "
            "reduced productivity feeds back into the next day's discharge. "
            "Report where it settles and the first day it crosses the threshold."),
    )
    res.assume("evacuation_drop_pct", drop,
               "the evacuation shortfall under study", SOURCE_PARAMETER)
    res.assume("occupancy_to_productivity_curve",
               f"linear, 1.0 at or below {threshold:.0%} utilisation, "
               f"falling to {1 - slope:.2f} at 100%",
               "no dataset available here measures re-handle rates against yard "
               "utilisation, so the SHAPE of this relationship is assumed even "
               "though the volumes driving it are measured. The result should be "
               "read as a direction and a tipping point, not as a precise TEU "
               "forecast.", SOURCE_ASSUMED)

    rows, trace = await repo.rail_road_daily(from_date=from_date, to_date=to_date,
                                             terminal=terminal)
    res.trace(trace)
    if not rows:
        return res.note(
            f"core.perf_daily_traffic carries no rows for "
            f"{from_date.isoformat()}..{to_date.isoformat()} — the loop needs "
            "measured discharge and evacuation volumes to start from, and none "
            "are invented.", blocks_answer=True)

    def total(key: str) -> float:
        return sum(float(r.get(key) or 0) for r in rows)

    days_observed = max(1, len({r.get("report_date") for r in rows if r.get("report_date")}))
    nominal_discharge = round(total("imp_teus") / days_observed, 1)
    rail = round(total("rail_total_teus") / days_observed, 1)
    road = round((total("total_teus") - total("rail_total_teus")) / days_observed, 1)
    nominal_evacuation = round(rail + road, 1)

    if not nominal_discharge or not nominal_evacuation:
        return res.note(
            "the traffic rows carry no usable import or evacuation volume "
            "(imp_teus / total_teus / rail_total_teus are empty), so the loop has "
            "no measured starting point.", blocks_answer=True)

    res.assume("nominal_discharge_teu_per_day", nominal_discharge,
               f"mean daily import volume measured over {days_observed} observed "
               "day(s)", SOURCE_MEASURED)
    res.assume("nominal_evacuation_teu_per_day", nominal_evacuation,
               f"mean daily rail + road evacuation measured over {days_observed} "
               "observed day(s)", SOURCE_DERIVED)

    capacity = float(params.get("yard_capacity_teu") or 0) or round(
        max(total("imp_teus"), nominal_discharge * 10), 1)
    if not params.get("yard_capacity_teu"):
        res.assume("yard_capacity_teu", capacity,
                   "no rated yard capacity exists in this database and no "
                   "per-block occupancy is published, so capacity is scaled from "
                   "the observed import volume. Supply yard_capacity_teu to "
                   "replace this with the real figure — it moves the tipping day.",
                   SOURCE_ASSUMED)
    else:
        res.assume("yard_capacity_teu", capacity, "supplied in the request",
                   SOURCE_PARAMETER)

    opening = round(capacity * threshold * 0.8, 1)
    res.assume("opening_occupancy_teu", opening,
               "the yard is taken to start comfortably below the threshold; the "
               "corpus carries inventory snapshots but not a reconciled opening "
               "position for this window.", SOURCE_ASSUMED)

    stressed = simulate(
        opening_occupancy=opening, capacity=capacity,
        nominal_discharge=nominal_discharge, nominal_evacuation=nominal_evacuation,
        evacuation_drop=drop, threshold=threshold, slope=slope, days=horizon)
    baseline = simulate(
        opening_occupancy=opening, capacity=capacity,
        nominal_discharge=nominal_discharge, nominal_evacuation=nominal_evacuation,
        evacuation_drop=0.0, threshold=threshold, slope=slope, days=horizon)

    tipped = tipping_day(stressed, threshold)
    settled = converged(stressed)
    final = stressed[-1]
    mode = regime(nominal_discharge=nominal_discharge,
                  nominal_evacuation=nominal_evacuation,
                  evacuation_drop=drop, slope=slope)
    full_on = saturation_day(stressed)
    blocked_total = round(sum(r["discharge_blocked"] for r in stressed), 1)

    res.result = {
        "window": {"from": from_date, "to": to_date, "days_observed": days_observed},
        "baseline_rates": {"discharge_teu_per_day": nominal_discharge,
                           "evacuation_teu_per_day": nominal_evacuation,
                           "rail_teu_per_day": rail, "road_teu_per_day": road},
        "yard": {"capacity_teu": capacity, "opening_teu": opening,
                 "threshold": threshold},
        "with_shortfall": stressed,
        "without_shortfall": baseline,
        "tipping_day": tipped,
        "converged_at": settled,
        "regime": mode,
        "yard_full_on_day": full_on,
    }
    res.figures = {
        "regime": mode,
        "evacuation_drop_pct": round(drop * 100, 1),
        "tipping_day": tipped,
        "yard_full_on_day": full_on,
        "discharge_blocked_teu": blocked_total,
        "days_to_threshold": tipped,
        "final_utilisation_pct": round(final["utilisation"] * 100, 1),
        "final_utilisation_pct_baseline": round(baseline[-1]["utilisation"] * 100, 1),
        "final_productivity_loss_pct": final["productivity_loss_pct"],
        "converged_utilisation_pct": (round(settled["utilisation"] * 100, 1)
                                      if settled else None),
        "converged_on_day": settled["day"] if settled else None,
        "cumulative_discharge_lost_teu": round(
            sum(b["discharged"] - s["discharged"]
                for b, s in zip(baseline, stressed)), 1),
        "berth_productivity_at_horizon_pct_of_normal": round(
            final["productivity_factor"] * 100, 1),
    }

    if tipped:
        res.recommend(
            "ACT_BEFORE_DAY_" + str(tipped),
            f"utilisation crosses {threshold:.0%} on day {tipped}; before that "
            "the yard absorbs the shortfall for free, after it every further day "
            "costs berth productivity as well as yard space",
            tipping_day=tipped)
    res.recommend(
        "RESTORE_EVACUATION_BEFORE_ADDING_BERTH_CAPACITY",
        "the loop is closed through evacuation, not through the quay: adding "
        f"crane capacity while evacuation is {drop:.0%} short raises discharge "
        "into a yard that cannot release it and tightens the loop",
        evacuation_shortfall_pct=round(drop * 100, 1))
    if mode == "SATURATING":
        res.recommend(
            "TREAT_AS_A_BERTH_PROBLEM_ONCE_FULL",
            f"the yard fills to capacity"
            + (f" on day {full_on}" if full_on else " within the horizon")
            + f" and {blocked_total:g} TEU of discharge is blocked over the "
              "horizon; from that point the constraint is quay-side, and vessels "
              "wait rather than the yard growing",
            yard_full_on_day=full_on, discharge_blocked_teu=blocked_total)
        res.note(
            "REGIME: SATURATING. The productivity collapse is not deep enough to "
            "pull discharge below evacuation "
            f"({nominal_discharge:g} x {1 - slope:.2f} = "
            f"{nominal_discharge * (1 - slope):g} TEU/day against "
            f"{nominal_evacuation * (1 - drop):g} TEU/day leaving), so there is no "
            "equilibrium below capacity. The yard fills until it is physically "
            "full and the surplus becomes a vessel-side backlog. Intervention is "
            "not optional here — the loop does not stop itself.")
    else:
        res.note(
            "REGIME: CONVERGING. The loop is self-limiting, which is not the same "
            "as safe: it settles by throttling the berth. The cost appears as "
            "vessel turnaround, not as a yard alarm, so it will be visible in UC-1 "
            "before it is visible here.")
    return res
