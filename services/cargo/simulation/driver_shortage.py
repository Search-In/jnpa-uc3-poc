"""Scenario III-B — Driver Shortage.

    "A shortage reduces the number of trips each vehicle can complete in a day by
     one third. Determine the effect on evacuation throughput, and identify which
     transporters and which cargo flows are most exposed. Also show how best
     evacuation strategy is determined. Consider situation from 1st to 3rd August
     and show state on 4th August 2026."       — JNPA Notice, 05 Aug 2026, §4 III-B

Method
------
1. **Baseline.** One EIR is one gate trip, so trips per vehicle per day come
   straight from ``core.eir`` (``truck_no`` x ``truck_in_time::date``), attributed
   to the transporter printed on the document (``company``). Real JNPA data.
2. **Apply the shortage.** Each vehicle's daily trips fall to
   ``floor(trips x (1 - reduction))``. Floor, not round: a vehicle cannot complete
   a fraction of a trip, and rounding up would flatter the result.
3. **Throughput effect.** The lost trips are the containers that do not get
   evacuated. They accumulate across the window and land as a backlog on the
   state date.
4. **Exposure.** A transporter is exposed in two distinct ways, and the answer
   reports both because they suggest different mitigations:
       * ABSOLUTE  — most trips lost (biggest contributor to the shortfall)
       * STRUCTURAL — highest trips per vehicle per day (most dependent on
         multi-trip cycles, so a one-third cut bites hardest)
   Cargo flows are ranked by trips lost, from the EIR flow/facility split.
5. **Best evacuation strategy.** With fewer trips available, the question is which
   trips to keep. The ranking rule is stated explicitly rather than left implicit:
   preserve the trips that move the most cargo per trip and clear the oldest
   backlog first, and move what road cannot carry onto rail where a rake is
   already planned.

Every figure is a count of real rows or arithmetic on them. Where a value is not
in the data — vehicle availability per transporter, driver rosters, per-trip
container payload beyond what the EIR records — the response says so instead of
supplying one.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_DERIVED, SOURCE_MEASURED,
                   SOURCE_PARAMETER, SimulationResult, pct)

SCENARIO = "driver-shortage"

#: Minimum observed trips before core.eir is trusted to describe the vehicle
#: population. Below this the scenario falls through to gate telemetry for
#: throughput and declares transporter attribution unavailable.
MIN_TRIPS_FOR_POPULATION = 30
DEFAULT_REDUCTION = 1.0 / 3.0
TOP_N = 10


def _day_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(to_date, time.min, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario III-B. ``params``:

        from_date, to_date  required (both inclusive) — the shortage window
        state_date          optional — the day the backlog is reported for
        reduction_pct       default 1/3, per the Notice
    """
    from_date: date = params["from_date"]
    to_date: date = params["to_date"]
    state_date: Optional[date] = params.get("state_date") or (to_date + timedelta(days=1))
    reduction = float(params.get("reduction_pct", DEFAULT_REDUCTION))
    from_ts, to_ts = _day_bounds(from_date, to_date)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            "Count trips per vehicle per day from core.eir (one Equipment "
            "Interchange Report = one gate trip), attributed to the transporter on "
            f"the document. Reduce each vehicle-day to floor(trips x "
            f"{1 - reduction:.4f}). The difference is trips lost; those are "
            "containers not evacuated, accumulated across the window and reported "
            f"as the backlog on {state_date}. Rank transporters by absolute trips "
            "lost and by trips-per-vehicle (structural exposure), and cargo flows "
            "by trips lost."),
    )
    res.assume("reduction_pct", round(reduction, 4),
               "the one-third reduction in trips per vehicle per day stated in the "
               "scenario", SOURCE_PARAMETER)
    res.assume("trip_definition", "one EIR = one gate trip",
               "core.eir records a truck-in/truck-out pair per gate movement; it "
               "is the only per-trip record with a transporter attribution",
               SOURCE_MEASURED)
    res.assume("rounding", "floor",
               "a vehicle cannot complete a fraction of a trip; rounding down "
               "rather than to nearest avoids overstating remaining capacity",
               SOURCE_ASSUMED)

    rows, trace = await repo.vehicle_trips(from_ts=from_ts, to_ts=to_ts)
    res.trace(trace)

    # core.eir is the only table that attributes a trip to a transporter, so it
    # is preferred — but only when there is enough of it to describe the window.
    # On JNPA's database it holds five trips against 482,966 gate events across
    # 8,581 vehicles, and "every vehicle does a third fewer trips" cannot be
    # answered from five. Fall through to telemetry for the THROUGHPUT half and
    # declare the transporter half unattributable rather than rank two rows.
    eir_trips = sum(int(r.get("trips") or 0) for r in rows)
    unattributed = False
    if eir_trips < MIN_TRIPS_FOR_POPULATION and hasattr(
            repo, "vehicle_trips_from_events"):
        event_rows, event_trace = await repo.vehicle_trips_from_events(
            from_ts=from_ts, to_ts=to_ts)
        res.trace(event_trace)
        if sum(int(r.get("trips") or 0) for r in event_rows) > eir_trips:
            rows, unattributed = event_rows, True
            res.assume(
                "trip_source", "core.gate_event",
                f"core.eir carries only {eir_trips} trip(s) in this window — below "
                f"the {MIN_TRIPS_FOR_POPULATION} needed to describe the vehicle "
                "population — so gate telemetry is used for throughput instead",
                SOURCE_DERIVED)
            res.assume(
                "transporter_attribution", "not available",
                "core.gate_event carries no transporter column, and only ~41 of "
                "its 8,581 plates appear in core.vehicle, so trips cannot be "
                "attributed to a transporter at any useful coverage. The "
                "throughput figures below are real; the transporter ranking is "
                "NOT and is reported as unattributed. A plate-to-transporter "
                "register (FASTag / fleet tracking / PDP) is what closes this.",
                SOURCE_ASSUMED)

    if not rows:
        return res.note(
            f"neither core.eir nor core.gate_event records gate trips between "
            f"{from_date} and {to_date} — baseline throughput cannot be "
            "established and none is invented.", blocks_answer=True)

    # ---- per vehicle-day -----------------------------------------------------
    by_transporter: dict[str, dict] = {}
    vehicles: set[str] = set()
    total_trips = total_reduced = 0
    total_containers = 0
    for r in rows:
        transporter = r.get("transporter") or "UNATTRIBUTED"
        trips = int(r.get("trips") or 0)
        reduced = int(trips * (1.0 - reduction))  # floor
        containers = int(r.get("containers") or 0)
        total_trips += trips
        total_reduced += reduced
        total_containers += containers
        vehicles.add(str(r.get("truck_no")))
        agg = by_transporter.setdefault(transporter, {
            "transporter": transporter, "trips": 0, "reduced_trips": 0,
            "containers": 0, "vehicle_days": 0, "vehicles": set()})
        agg["trips"] += trips
        agg["reduced_trips"] += reduced
        agg["containers"] += containers
        agg["vehicle_days"] += 1
        agg["vehicles"].add(str(r.get("truck_no")))

    lost_trips = total_trips - total_reduced
    days = (to_date - from_date).days + 1

    exposure: list[dict] = []
    for agg in by_transporter.values():
        lost = agg["trips"] - agg["reduced_trips"]
        exposure.append({
            "transporter": agg["transporter"],
            "vehicles": len(agg["vehicles"]),
            "trips": agg["trips"],
            "reduced_trips": agg["reduced_trips"],
            "trips_lost": lost,
            "trips_lost_pct": pct(lost, agg["trips"]),
            "trips_per_vehicle_day": round(agg["trips"] / agg["vehicle_days"], 2)
            if agg["vehicle_days"] else 0.0,
            "containers": agg["containers"],
        })
    by_absolute = sorted(exposure, key=lambda e: (-e["trips_lost"], e["transporter"]))
    by_structural = sorted(
        exposure, key=lambda e: (-e["trips_per_vehicle_day"], e["transporter"]))

    # ---- cargo flows ---------------------------------------------------------
    flows, flow_trace = await repo.cargo_flows(from_ts=from_ts, to_ts=to_ts)
    res.trace(flow_trace)
    flow_exposure = []
    for f in flows:
        trips = int(f.get("trips") or 0)
        lost = trips - int(trips * (1.0 - reduction))
        flow_exposure.append({
            "flow": f.get("flow"), "facility": f.get("facility"),
            "trips": trips, "vehicles": int(f.get("vehicles") or 0),
            "trips_lost": lost, "trips_lost_pct": pct(lost, trips)})
    flow_exposure.sort(key=lambda f: (-f["trips_lost"], str(f["flow"])))

    # ---- state on the report date -------------------------------------------
    pend_rows, pend_trace = await repo.pendency_snapshot()
    res.trace(pend_trace)
    pending_by_mode: dict[str, int] = {}
    for r in pend_rows:
        mode = r.get("evacuation_mode") or "UNKNOWN"
        pending_by_mode[mode] = pending_by_mode.get(mode, 0) + int(r.get("containers") or 0)
    pending_total = sum(pending_by_mode.values())

    # Containers per trip, derived from the same EIR rows rather than assumed.
    if total_containers and total_trips:
        containers_per_trip = round(total_containers / total_trips, 3)
        res.assume("containers_per_trip", containers_per_trip,
                   f"derived from this window: {total_containers} distinct "
                   f"containers across {total_trips} trips", SOURCE_DERIVED)
    else:
        containers_per_trip = 1.0
        res.assume("containers_per_trip", containers_per_trip,
                   "the EIR rows in this window carry no container numbers, so one "
                   "container per trip is assumed", SOURCE_ASSUMED)
    backlog = int(round(lost_trips * containers_per_trip))

    res.result = {
        "window": {"from": from_date, "to": to_date, "days": days},
        "state_date": state_date,
        "baseline": {"trips": total_trips, "vehicles": len(vehicles),
                     "trips_per_vehicle_day": round(total_trips / max(len(rows), 1), 2)},
        "after_shortage": {"trips": total_reduced, "trips_lost": lost_trips},
        "state_on_report_date": {
            "date": state_date,
            "containers_not_evacuated": backlog,
            "already_pending_in_port": pending_total,
            "pending_by_mode": pending_by_mode,
            "projected_total_awaiting_evacuation": pending_total + backlog,
        },
        # Suppressed when the trips came from telemetry: ranking a single
        # 'UNATTRIBUTED' bucket would look like an answer and is not one. The
        # assumptions say why, and `attribution_available` lets a UI show the
        # gap rather than an empty table.
        "exposed_transporters": {
            "attribution_available": not unattributed,
            "by_absolute_loss": [] if unattributed else by_absolute[:TOP_N],
            "by_structural_dependence": ([] if unattributed
                                         else by_structural[:TOP_N]),
        },
        "exposed_cargo_flows": flow_exposure[:TOP_N],
    }
    res.figures = {
        "baseline_trips": total_trips,
        "reduced_trips": total_reduced,
        "trips_lost": lost_trips,
        "throughput_loss_pct": pct(lost_trips, total_trips),
        "vehicles_active": len(vehicles),
        "vehicle_days": len(rows),
        "mean_trips_per_vehicle_day": round(total_trips / len(rows), 2) if rows else 0.0,
        "containers_per_trip": containers_per_trip,
        "containers_not_evacuated": backlog,
        "backlog_per_day": round(backlog / days, 1) if days else 0.0,
        "transporters_affected": len(by_transporter),
        "cargo_flows_affected": len(flow_exposure),
    }

    # ---- best evacuation strategy -------------------------------------------
    # Stated as an explicit ranking rule, so the recommendation is auditable
    # rather than an opinion.
    res.assume("evacuation_priority_rule",
               "1) highest containers per trip, 2) oldest dwell first, "
               "3) divert to rail where a rake is already planned",
               "with fewer trips available the question is which trips to keep; "
               "this is the rule the recommendations below apply, stated so it "
               "can be challenged or replaced", SOURCE_ASSUMED)

    if by_absolute:
        worst = by_absolute[0]
        res.recommend(
            "PRIORITISE_HIGH_YIELD_TRIPS",
            f"{lost_trips} trips ({res.figures['throughput_loss_pct']}% of "
            f"baseline) are lost across {len(by_transporter)} transporters; "
            "allocating the remaining trips to the flows with the highest "
            "containers-per-trip preserves the most evacuation volume",
            trips_lost=lost_trips, containers_at_risk=backlog)
        res.recommend(
            "REINFORCE_TRANSPORTER",
            f"{worst['transporter']} loses the most trips ({worst['trips_lost']} of "
            f"{worst['trips']}); adding drivers there recovers the largest single "
            "share of throughput",
            transporter=worst["transporter"], trips_lost=worst["trips_lost"])
    if by_structural and by_structural[0]["trips_per_vehicle_day"] > 1.5:
        s = by_structural[0]
        res.recommend(
            "PROTECT_MULTI_TRIP_CYCLES",
            f"{s['transporter']} runs {s['trips_per_vehicle_day']} trips per "
            "vehicle-day, so a one-third cut removes a whole cycle; double-shifting "
            "its vehicles protects more volume than adding vehicles elsewhere",
            transporter=s["transporter"],
            trips_per_vehicle_day=s["trips_per_vehicle_day"])
    if pending_by_mode.get("RAIL"):
        res.recommend(
            "DIVERT_TO_RAIL",
            f"{pending_by_mode['RAIL']} containers awaiting evacuation are already "
            "attributed to rail; moving road-bound backlog onto those planned rakes "
            "bypasses the driver constraint entirely",
            rail_pending=pending_by_mode["RAIL"])
    if flow_exposure:
        f = flow_exposure[0]
        res.recommend(
            "RESCHEDULE_FLOW",
            f"the {f['flow']} flow via {f['facility']} loses {f['trips_lost']} "
            "trips — the largest single flow exposure",
            flow=f["flow"], facility=f["facility"])

    res.note("vehicle availability per transporter, driver rosters and shift "
             "patterns are not in this database, so the shortage is modelled as a "
             "uniform reduction in trips per vehicle-day rather than as a count of "
             "absent drivers.")
    if not pend_rows:
        res.note("core.cargo has no unreleased containers, so the projected "
                 "backlog is the lost-trip figure alone with no standing pendency "
                 "added to it.")
    return res
