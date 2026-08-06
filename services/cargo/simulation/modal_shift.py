"""Scenario II-A — Rail to Road Modal Shift.

    "Twenty per cent of containers currently evacuated by rail are moved to road
     instead for period 1st August 2026 to 3rd August 2026. Determine whether the
     gate absorbs the additional load. Present the hourly gate profile before and
     after the shift, and identify the first constraint to saturate."
                                                 — JNPA Notice, 05 Aug 2026, §3 II-A

Method
------
1. **How much rail volume is there?** ``core.perf_daily_traffic`` carries real
   JNPA daily figures per terminal: ``rail_total_teus`` and ``total_teus``. That
   is the authoritative rail volume for the window.
2. **How many extra TRUCKS is 20% of it?** TEUs are not trucks, so the conversion
   is derived from the same window rather than assumed:

       teu_per_trip = road_teus_in_window / observed_gate_trips_in_window
       extra_trips  = (rail_teus x shift_pct) / teu_per_trip

   When the road TEU total or the trip count is missing, a declared fallback of
   1 TEU per trip is used and flagged — it understates the truck count for a
   40-foot-heavy flow, and the response says so.
3. **Baseline hourly gate profile** from ``core.eir.truck_in_time`` — real gate
   paperwork, over an arbitrary historical window. (The only pre-existing hourly
   gate view, ``mart.v_gate_throughput``, is pinned to ``now() - 24h`` and cannot
   address 1-3 August at all.)
4. **Shifted profile.** The extra trips are distributed across hours *in
   proportion to the observed arrival shape* — the shifted boxes are assumed to
   arrive like the boxes already arriving. That is an assumption, and an
   alternative (uniform spread) would give a lower peak, so it is declared.
5. **First constraint to saturate.** Each candidate ceiling is evaluated against
   the shifted profile and the one breached in the earliest hour wins:
       * gate throughput  — the sustained hourly rate (shared with III-A)
       * appointment slots — declared TAS capacity for the hour
   Ceilings the database cannot measure (yard ground slots, parking, road
   corridor) are named in ``notes`` rather than silently omitted.

The sustained-rate definition is imported from :mod:`.gate_slotting` so the two
gate scenarios cannot drift apart.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from .base import (SOURCE_ASSUMED, SOURCE_DERIVED, SOURCE_MEASURED,
                   SOURCE_PARAMETER, SimulationResult, pct)
from .gate_slotting import derive_sustained_rate, load_profile

SCENARIO = "modal-shift"
DEFAULT_SHIFT_PCT = 0.20
#: Used only when the window carries no road TEU total or no observed trips.
FALLBACK_TEU_PER_TRIP = 1.0


def _day_bounds(from_date: date, to_date: date) -> tuple[datetime, datetime]:
    """[00:00 on from_date, 00:00 on the day AFTER to_date) in UTC — the window is
    inclusive of both dates, which is how the Notice phrases "1st to 3rd"."""
    start = datetime.combine(from_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(to_date, time.min, tzinfo=timezone.utc) + timedelta(days=1)
    return start, end


async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario II-A. ``params``:

        from_date, to_date   required (dates; both inclusive)
        shift_pct            default 0.20, per the Notice
        terminal, gate_id    optional scoping
        sustained_rate       optional override of the gate ceiling
    """
    from_date: date = params["from_date"]
    to_date: date = params["to_date"]
    shift_pct = float(params.get("shift_pct", DEFAULT_SHIFT_PCT))
    terminal = (params.get("terminal") or "").strip() or None
    gate_id = (params.get("gate_id") or "").strip() or None
    from_ts, to_ts = _day_bounds(from_date, to_date)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            f"Take rail volume for {from_date}..{to_date} from "
            "core.perf_daily_traffic (rail_total_teus). Shift "
            f"{pct(shift_pct, 1.0, digits=0)}% of it to road. Convert TEU to truck "
            "trips using teu_per_trip derived from the same window "
            "(road TEU / observed gate trips). Build the baseline hourly gate "
            "profile from core.eir.truck_in_time, add the extra trips in "
            "proportion to the observed arrival shape, then test the shifted "
            "profile against each measurable ceiling (gate sustained rate, "
            "declared TAS slot capacity) and report the one breached earliest."),
    )
    res.assume("shift_pct", shift_pct,
               "the modal shift stated in the scenario", SOURCE_PARAMETER)

    # ---- 1. rail volume -----------------------------------------------------
    daily, trace = await repo.rail_road_daily(from_date=from_date, to_date=to_date,
                                              terminal=terminal)
    res.trace(trace)
    rail_teus = sum(float(r.get("rail_total_teus") or 0) for r in daily)
    total_teus = sum(float(r.get("total_teus") or 0) for r in daily)
    road_teus = max(total_teus - rail_teus, 0.0)

    # ---- 2. per-container corroboration (migration 0128) --------------------
    split, split_trace = await repo.evacuation_mode_split(from_ts=from_ts, to_ts=to_ts)
    res.trace(split_trace)
    by_mode: dict[str, int] = {}
    derived_labels = 0
    for row in split:
        mode = row.get("evacuation_mode") or "UNKNOWN"
        by_mode[mode] = by_mode.get(mode, 0) + int(row.get("containers") or 0)
        if row.get("source") in ("RAKE_PLAN", "JOB_ASSIGNMENT", "LDB_MOVEMENT"):
            derived_labels += int(row.get("containers") or 0)
    labelled = sum(by_mode.values())
    unknown_share = pct(by_mode.get("UNKNOWN", 0), labelled) if labelled else 0.0
    if labelled and unknown_share > 0:
        res.assume("evacuation_mode_unknown_share_pct", unknown_share,
                   f"{by_mode.get('UNKNOWN', 0)} of {labelled} containers in the "
                   "window carry no rail/road attribution (migration 0128 could "
                   "not derive one). They are excluded from the per-container "
                   "cross-check rather than assigned to a mode",
                   SOURCE_DERIVED)
    if derived_labels:
        res.assume("evacuation_mode_provenance", "DERIVED",
                   f"{derived_labels} container labels were derived by migration "
                   "0128 from rake plans / job assignments / LDB movements, not "
                   "declared by JNPA", SOURCE_DERIVED)

    if not daily and not labelled:
        return res.note(
            "neither core.perf_daily_traffic nor core.cargo carries rail volume "
            f"for {from_date}..{to_date} — the shifted volume cannot be sized and "
            "no figure is invented.", blocks_answer=True)
    if not daily:
        return res.note(
            "core.perf_daily_traffic has no DAY rows for this window, so rail TEU "
            "volume is unknown. The per-container split is present but counts "
            "boxes, not TEU, and cannot be converted without the daily report.",
            blocks_answer=True)

    # ---- 3. baseline hourly gate profile ------------------------------------
    profile, traces, prof_assumptions, source = await load_profile(
        repo, from_ts=from_ts, to_ts=to_ts, terminal=terminal, gate_id=gate_id)
    res.trace_all(traces)
    for a in prof_assumptions:
        res.assumptions.append(a)
    if not profile:
        return res.note(
            f"no gate arrivals recorded for {from_date}..{to_date} in core.eir or "
            "core.gate_event — the before/after hourly profile cannot be built.",
            blocks_answer=True)
    res.assume("arrival_source", source,
               "the table the baseline hourly profile was read from", SOURCE_MEASURED)

    observed_trips = sum(h["arrivals"] for h in profile)

    # ---- 4. TEU -> trips ----------------------------------------------------
    if road_teus > 0 and observed_trips > 0:
        teu_per_trip = round(road_teus / observed_trips, 3)
        res.assume("teu_per_trip", teu_per_trip,
                   f"derived from this window: {road_teus:.0f} road TEU "
                   f"(total_teus - rail_total_teus) over {observed_trips} observed "
                   "gate trips", SOURCE_DERIVED)
    else:
        teu_per_trip = FALLBACK_TEU_PER_TRIP
        res.assume("teu_per_trip", teu_per_trip,
                   "road TEU or observed trip count is missing for this window, so "
                   "one TEU per trip is assumed. This UNDERSTATES the truck count "
                   "wherever 40ft boxes dominate — a 2 TEU box would halve it",
                   SOURCE_ASSUMED)

    shifted_teus = round(rail_teus * shift_pct, 1)
    extra_trips = int(round(shifted_teus / teu_per_trip)) if teu_per_trip else 0

    # ---- 5. distribute the extra trips --------------------------------------
    res.assume("shift_arrival_shape", "proportional to observed",
               "the shifted boxes are assumed to arrive across the day in the "
               "same shape as the trucks already arriving. A uniform spread would "
               "produce a lower peak and a later first constraint, so this is the "
               "more conservative of the two readings", SOURCE_ASSUMED)
    shifted_profile: list[dict] = []
    allocated = 0
    for idx, hour in enumerate(profile):
        share = (hour["arrivals"] / observed_trips) if observed_trips else 0.0
        # Largest-remainder on the last bucket so the extra trips sum exactly.
        add = (extra_trips - allocated if idx == len(profile) - 1
               else int(extra_trips * share))
        allocated += add
        shifted_profile.append({
            "bucket": hour["bucket"],
            "baseline": hour["arrivals"],
            "added": add,
            "shifted": hour["arrivals"] + add,
        })

    # ---- 6. ceilings --------------------------------------------------------
    rate, cap_trace, rate_assumption = await derive_sustained_rate(
        repo, profile, from_ts=from_ts, to_ts=to_ts, gate_id=gate_id,
        override=params.get("sustained_rate"))
    res.trace(cap_trace)
    res.assumptions.append(rate_assumption)

    tas_rows, tas_trace = await repo.tas_hourly_capacity(from_ts=from_ts, to_ts=to_ts,
                                                         gate_id=gate_id)
    res.trace(tas_trace)
    tas_by_hour = {r["bucket"]: float(r["slot_capacity"] or 0) for r in tas_rows}

    breaches: list[dict] = []
    for hour in shifted_profile:
        if rate and hour["shifted"] > rate:
            breaches.append({"hour": hour["bucket"], "constraint": "GATE_THROUGHPUT",
                             "ceiling": rate, "load": hour["shifted"],
                             "excess": round(hour["shifted"] - rate, 1)})
        cap = tas_by_hour.get(hour["bucket"])
        if cap and hour["shifted"] > cap:
            breaches.append({"hour": hour["bucket"], "constraint": "APPOINTMENT_SLOTS",
                             "ceiling": cap, "load": hour["shifted"],
                             "excess": round(hour["shifted"] - cap, 1)})
    breaches.sort(key=lambda b: (b["hour"], b["constraint"]))
    first_constraint = breaches[0] if breaches else None

    baseline_over = [h for h in profile if rate and h["arrivals"] > rate]
    shifted_over = [h for h in shifted_profile if rate and h["shifted"] > rate]
    baseline_peak = max((h["arrivals"] for h in profile), default=0)
    shifted_peak = max((h["shifted"] for h in shifted_profile), default=0)

    if not tas_by_hour:
        res.note("core.tas_appointment has no provisioned windows for this period, "
                 "so the appointment-slot ceiling could not be tested; only the "
                 "gate throughput ceiling was evaluated.")
    res.note("yard ground-slot capacity, truck parking and the road corridor are "
             "also candidate constraints, but this database carries no hourly "
             "measure of them, so they are not evaluated here.")

    res.result = {
        "window": {"from": from_date, "to": to_date},
        "terminal": terminal,
        "rail_volume": {"rail_teus": rail_teus, "total_teus": total_teus,
                        "road_teus": road_teus,
                        "rakes": sum(int(r.get("rakes") or 0) for r in daily)},
        "container_mode_split": by_mode,
        "baseline_profile": [{"hour": h["bucket"], "arrivals": h["arrivals"]}
                             for h in profile],
        "shifted_profile": [{"hour": h["bucket"], "baseline": h["baseline"],
                             "added": h["added"], "shifted": h["shifted"]}
                            for h in shifted_profile],
        "saturated_hours": [b for b in breaches if b["constraint"] == "GATE_THROUGHPUT"],
        "first_constraint": first_constraint,
        "gate_absorbs_load": not shifted_over,
    }
    res.figures = {
        "rail_teus_in_window": rail_teus,
        "shifted_teus": shifted_teus,
        "teu_per_trip": teu_per_trip,
        "additional_truck_trips": extra_trips,
        "baseline_trips": observed_trips,
        "shifted_trips": observed_trips + extra_trips,
        "trip_increase_pct": pct(extra_trips, observed_trips),
        "sustained_rate_per_hour": rate,
        "baseline_peak": baseline_peak,
        "shifted_peak": shifted_peak,
        "peak_increase_pct": pct(shifted_peak - baseline_peak, baseline_peak),
        "saturated_hours_before": len(baseline_over),
        "saturated_hours_after": len(shifted_over),
        "additional_saturated_hours": len(shifted_over) - len(baseline_over),
    }

    if shifted_over:
        res.recommend(
            "STAGGER_SHIFTED_ARRIVALS",
            f"the shift adds {extra_trips} trips and saturates "
            f"{len(shifted_over)} hours (up from {len(baseline_over)}); booking the "
            "shifted boxes into the hours already running below "
            f"{rate}/h avoids the new peak entirely",
            additional_trips=extra_trips, saturated_hours=len(shifted_over))
        if first_constraint:
            res.recommend(
                "RELIEVE_FIRST_CONSTRAINT",
                f"{first_constraint['constraint']} is breached first, at "
                f"{first_constraint['hour']}, by {first_constraint['excess']} trucks",
                constraint=first_constraint["constraint"],
                hour=first_constraint["hour"])
    else:
        res.recommend(
            "ABSORBED",
            f"the gate absorbs all {extra_trips} additional trips: no hour exceeds "
            f"the sustained rate of {rate}/h after the shift")
    return res
