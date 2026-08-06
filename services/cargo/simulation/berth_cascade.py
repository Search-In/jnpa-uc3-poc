"""Scenario I-B — Extended Berth Window.

    "On 2nd August 2026, a vessel's operation is overrun by six hours. Identify
     which subsequent calls at that terminal are displaced, by how long, and state
     the cumulative delay across the berth queue over the following forty-eight
     hours."                                      — JNPA Notice, 05 Aug 2026, §2 I-B

Method
------
1. Read every call at the terminal whose operation window opens inside the
   48-hour horizon (``core.berthing_record``), ordered by start time.
2. Give each call a start, an end and a duration. Start is the first of
   ``cargo_operation_start`` / ``berthing_time`` / ``ata`` / ``eta`` that is
   present; end is ``cargo_operation_end`` / ``departure_time``.
3. Extend the target call's end by ``delay_hours``.
4. Cascade **per berth**: a berth serves one vessel at a time, so the next call at
   that berth cannot start before the previous one finishes. Walking the berth's
   calls in time order, ``new_start = max(original_start, previous_new_end)``.
5. Report each displaced call's original vs new time, its own delay, and the sum
   across the queue.

Calls at OTHER berths are untouched — that is the whole point of the per-berth
cascade, and it is stated as an assumption rather than left implicit.

:func:`cascade` is also imported by :mod:`.crane_productivity`, which converts a
productivity loss into a ``delta_hours`` and then asks exactly this question about
the queue behind the slowed vessel. One cascade, two scenarios.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Any, Optional, Sequence

from .base import (SOURCE_ASSUMED, SOURCE_DERIVED, SOURCE_PARAMETER,
                   SimulationResult, hours_between)

SCENARIO = "berth-cascade"
DEFAULT_HORIZON_HOURS = 48
UNASSIGNED_BERTH = "UNASSIGNED"


def _start_of(call: dict) -> Optional[datetime]:
    """Best available operation start: reported start, else berthing, else ATA,
    else ETA. Same COALESCE the repository orders by, so ordering and arithmetic
    can never disagree."""
    for key in ("cargo_operation_start", "berthing_time", "ata", "eta"):
        if call.get(key) is not None:
            return call[key]
    return None


def _end_of(call: dict) -> Optional[datetime]:
    for key in ("cargo_operation_end", "departure_time"):
        if call.get(key) is not None:
            return call[key]
    return None


def _identity(call: dict) -> dict:
    return {"berthing_record_id": call.get("id"),
            "vessel_name": call.get("vessel_name"),
            "voyage_number": call.get("voyage_number"),
            "berth_number": call.get("berth_number") or UNASSIGNED_BERTH,
            "terminal": call.get("terminal")}


def _matches(call: dict, *, berthing_record_id: Optional[int],
             vessel_name: Optional[str], voyage_number: Optional[str]) -> bool:
    if berthing_record_id is not None:
        return call.get("id") == berthing_record_id
    if vessel_name:
        if str(call.get("vessel_name") or "").strip().upper() != vessel_name.strip().upper():
            return False
        if voyage_number:
            return (str(call.get("voyage_number") or "").strip().upper()
                    == voyage_number.strip().upper())
        return True
    return False


def cascade(calls: Sequence[dict], *, target_index: int, delta_hours: float,
            default_duration_hours: Optional[float] = None) -> list[dict]:
    """Push the target call's end out by ``delta_hours`` and propagate the knock-on
    down its berth. Returns one row per call, in the input order.

    Pure and deterministic — no I/O, no clock — so both scenarios that use it can
    be tested without a database. Calls whose duration is unknown use
    ``default_duration_hours`` (the caller declares that as an assumption); when
    that is also None the call is passed through undisplaced and flagged."""
    plan: list[dict] = []
    for idx, call in enumerate(calls):
        start, end = _start_of(call), _end_of(call)
        duration = hours_between(start, end)
        assumed_duration = duration is None and default_duration_hours is not None
        if assumed_duration:
            duration = default_duration_hours
        plan.append({
            "index": idx, **_identity(call),
            "original_start": start, "original_end": end,
            "duration_hours": duration,
            "duration_assumed": assumed_duration,
            "is_target": idx == target_index,
            "new_start": start, "new_end": end, "delay_hours": 0.0,
        })

    # Group by berth, preserving time order within each berth.
    berths: dict[str, list[dict]] = {}
    for row in plan:
        berths.setdefault(row["berth_number"], []).append(row)

    target = plan[target_index]
    target_berth = target["berth_number"]

    for berth, rows in berths.items():
        rows.sort(key=lambda r: (r["original_start"] is None,
                                 r["original_start"] or datetime.min, r["index"]))
        previous_end: Optional[datetime] = None
        # Only the target's own berth inherits the overrun; every other berth is
        # replayed unchanged (previous_end tracking still runs, but nothing shifts
        # because no call there gains time).
        for row in rows:
            start, duration = row["original_start"], row["duration_hours"]
            if start is None:
                continue  # nothing to schedule against — passed through untouched
            new_start = start
            if previous_end is not None and previous_end > new_start:
                new_start = previous_end
            if row["is_target"]:
                new_end = ((row["original_end"] or new_start)
                           + timedelta(hours=delta_hours))
                # A target whose end shifts also shifts its own recorded delay.
                row["new_start"] = new_start
                row["new_end"] = new_end
                row["delay_hours"] = round(
                    (new_start - start).total_seconds() / 3600.0 + delta_hours, 2)
            else:
                if duration is None:
                    # Unknown duration and no default: cannot schedule what follows.
                    row["new_start"] = new_start
                    row["new_end"] = row["original_end"]
                    row["delay_hours"] = round(
                        (new_start - start).total_seconds() / 3600.0, 2)
                    previous_end = row["new_end"] or previous_end
                    continue
                new_end = new_start + timedelta(hours=duration)
                row["new_start"] = new_start
                row["new_end"] = new_end
                row["delay_hours"] = round(
                    (new_start - start).total_seconds() / 3600.0, 2)
            previous_end = row["new_end"]
        # Berths other than the target's can only self-conflict on the original
        # data; that is real information, so it is kept rather than zeroed.
        if berth != target_berth:
            for row in rows:
                row["cross_berth"] = True
    return plan


async def run(repo: Any, params: dict) -> SimulationResult:
    """Run scenario I-B. ``params``:

        terminal            required — the terminal whose queue is cascaded
        as_of               required — ISO timestamp; the horizon starts here
        delay_hours         the overrun (default 6.0, per the Notice)
        horizon_hours       default 48, per the Notice
        vessel_name         } identify the overrunning call; when none is given
        voyage_number       } the FIRST call in the window is used and that
        berthing_record_id  } choice is declared as an assumption
    """
    terminal = (params.get("terminal") or "").strip() or None
    as_of: datetime = params["as_of"]
    delay_hours = float(params.get("delay_hours", 6.0))
    horizon = int(params.get("horizon_hours", DEFAULT_HORIZON_HOURS))
    to_ts = as_of + timedelta(hours=horizon)

    res = SimulationResult(
        scenario=SCENARIO,
        method=(
            f"Read every vessel call at {terminal or 'all terminals'} whose "
            f"operation window opens in the {horizon}h from {as_of.isoformat()} "
            "(core.berthing_record). Extend the target call's operation end by "
            f"{delay_hours}h, then cascade down its berth: a berth serves one "
            "vessel at a time, so new_start = max(original_start, "
            "previous_new_end). Report each displaced call's delay and the sum "
            "across the queue."),
    )
    res.assume("delay_hours", delay_hours,
               "the overrun stated in the scenario", SOURCE_PARAMETER)
    res.assume("horizon_hours", horizon,
               "cumulative delay is reported over this window, per the Notice",
               SOURCE_PARAMETER)
    res.assume("berth_exclusivity", True,
               "one vessel occupies a berth at a time, so a call cannot start "
               "before the previous call at that berth completes; calls at other "
               "berths are unaffected", SOURCE_ASSUMED)

    calls, trace = await repo.berth_queue(terminal=terminal, from_ts=as_of, to_ts=to_ts)
    res.trace(trace)
    if not calls:
        return res.note(
            f"core.berthing_record returned no calls for {terminal or 'any terminal'} "
            f"between {as_of.isoformat()} and {to_ts.isoformat()} — the cascade "
            "cannot be computed and no figure is invented.", blocks_answer=True)

    # Resolve the target call.
    target_index: Optional[int] = None
    wanted = {"berthing_record_id": params.get("berthing_record_id"),
              "vessel_name": params.get("vessel_name"),
              "voyage_number": params.get("voyage_number")}
    if any(wanted.values()):
        for idx, call in enumerate(calls):
            if _matches(call, **wanted):
                target_index = idx
                break
        if target_index is None:
            return res.note(
                f"no call in the window matches {wanted} — nothing to overrun.",
                blocks_answer=True)
    else:
        target_index = 0
        res.assume("target_call", _identity(calls[0]),
                   "no vessel was named in the request, so the first call in the "
                   "window is treated as the overrunning one", SOURCE_ASSUMED)

    # Duration fallback for calls with no reported operation window.
    known = [h for h in (hours_between(_start_of(c), _end_of(c)) for c in calls)
             if h is not None]
    default_duration = round(median(known), 2) if known else None
    unknown_count = len(calls) - len(known)
    if unknown_count and default_duration is not None:
        res.assume("default_operation_hours", default_duration,
                   f"{unknown_count} of {len(calls)} calls report no operation "
                   "window; the median observed duration in the same window is "
                   "used so the queue behind them can still be scheduled",
                   SOURCE_DERIVED)
    elif unknown_count:
        res.note(f"{unknown_count} calls have no usable operation window and no "
                 "observed duration exists to stand in — they are passed through "
                 "undisplaced rather than given an invented duration.")

    plan = cascade(calls, target_index=target_index, delta_hours=delay_hours,
                   default_duration_hours=default_duration)

    displaced = [r for r in plan if r["delay_hours"] > 0 and not r["is_target"]]
    cumulative = round(sum(r["delay_hours"] for r in displaced), 2)
    target = plan[target_index]

    res.result = {
        "terminal": terminal,
        "window": {"from": as_of, "to": to_ts, "hours": horizon},
        "target_call": {k: target[k] for k in
                        ("berthing_record_id", "vessel_name", "voyage_number",
                         "berth_number", "original_end", "new_end")},
        "displaced_calls": [
            {"vessel": r["vessel_name"], "voyage": r["voyage_number"],
             "berth": r["berth_number"],
             "original_time": r["original_start"], "new_time": r["new_start"],
             "delay_hours": r["delay_hours"],
             "duration_assumed": r["duration_assumed"]}
            for r in displaced],
        "unaffected_calls": [
            {"vessel": r["vessel_name"], "voyage": r["voyage_number"],
             "berth": r["berth_number"]}
            for r in plan if r["delay_hours"] == 0 and not r["is_target"]],
    }
    res.figures = {
        "calls_in_window": len(calls),
        "calls_displaced": len(displaced),
        "cumulative_delay_hours": cumulative,
        "max_single_delay_hours": max((r["delay_hours"] for r in displaced), default=0.0),
        "mean_delay_hours": round(cumulative / len(displaced), 2) if displaced else 0.0,
        "target_delay_hours": target["delay_hours"],
        "berths_in_window": len({r["berth_number"] for r in plan}),
    }

    if displaced:
        worst = max(displaced, key=lambda r: r["delay_hours"])
        res.recommend(
            "RESEQUENCE_BERTH",
            f"{len(displaced)} calls at berth {target['berth_number']} inherit "
            f"{cumulative}h of delay; moving {worst['vessel_name']} to a free "
            "berth in the window removes the largest single displacement",
            berth=target["berth_number"], candidate=worst["vessel_name"],
            recovers_hours=worst["delay_hours"])
        res.recommend(
            "NOTIFY_DOWNSTREAM",
            "each displaced call's yard, gate and haulage bookings are keyed to "
            "its original window and need re-issuing",
            affected_vessels=[r["vessel"] for r in res.result["displaced_calls"]])
    else:
        res.recommend("NO_ACTION",
                      "the overrun is absorbed by existing slack at this berth — "
                      "no subsequent call in the horizon is displaced")
    return res
