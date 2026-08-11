"""Forward projection for scenario dates that lie beyond the corpus.

Why this exists
---------------
Two of the JNPA Notice scenarios are dated **6 August 2026**:

* I-A vessel bunching — *"On 6 August 2026 a large number of vessels are
  alongside across the port's berths..."*
* II-B equipment availability — *"Take up a vessel on 6th August 2026."*

The ground truth ends on **5 August 2026** (the last daily berthing report), and
bidder API access closed on 7 August 1700, so no later data will arrive. The
scenarios cannot be answered from measured rows.

The Notice anticipates exactly this and says what to do about it (§1.c):

    "Where the data does not carry a value your method requires, say so and state
     what you assumed in its place. An assumption declared openly will be treated
     more favourably than a figure presented without one."

So the answer is neither to refuse nor to quietly invent a 6 August: it is to
carry the last measured state forward by a stated rule, mark every row that came
from that rule, and put the rule in front of the reader.

The rule
--------
1. Find the last day the corpus actually covers (``measured_through``).
2. Take the calls still working at the end of that day — a call whose operation
   window has not closed is genuinely still there the next morning.
3. Add the expected arrivals for the projected day at the **measured** mean daily
   arrival rate over the corpus window. The rate is derived from real rows, not
   picked; the *identities* of those arrivals are unknown, so they enter as
   anonymous placeholders and are counted, never named.
4. Tag every carried or placeholder row ``data_origin='PROJECTED'``.

What this deliberately does not do
----------------------------------
It does not invent vessel names, voyage numbers, move counts or line
allegiances for arrivals that have not been declared. A projected arrival is a
unit of demand on a berth, nothing more. Anything that would require a name —
"which vessel should berth first" — is answered over the carried-forward calls,
which are real, with the projected arrivals shown as additional queue pressure.

The bunching premise is corroborated, not assumed
-------------------------------------------------
Vessels alongside on the five measured August days: 17, 22, 17, 22, 21 — with
BMCT holding 11 of 22 on 4 Aug and 12 of 21 on 5 Aug against 1-2 at APMT and
NSIGT. The Notice's premise ("a large number of vessels ... unevenly distributed
between terminals") is therefore visible in the measured data immediately before
the projected day, which is what makes the extrapolation defensible rather than
decorative.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .base import (SOURCE_DERIVED, SOURCE_MEASURED, Assumption, QueryTrace,
                   SimulationResult)

#: Assumption id used across every response that projects. Matches the suite-wide
#: register convention (A-01..A-06 are the deck's; A-07 is this one).
PROJECTION_ASSUMPTION_ID = "A-07"

#: How far back to look for the last measured day when the requested window is
#: empty. Wide enough to cross a quiet weekend, narrow enough that a genuinely
#: empty database still reports "no data" rather than reaching into last month.
LOOKBACK_DAYS = 14


class Coverage:
    """What the answer is standing on: measured rows, projected rows, or neither.

    Serialised into the response as ``coverage`` so the UI can render the banner
    from a field rather than by pattern-matching a note string."""

    def __init__(self, *, requested: datetime,
                 measured_through: Optional[datetime] = None,
                 basis: str = "MEASURED",
                 carried_calls: int = 0,
                 projected_arrivals: int = 0,
                 arrival_rate_per_day: Optional[float] = None) -> None:
        self.requested = requested
        self.measured_through = measured_through
        self.basis = basis                      # MEASURED | PROJECTED | NONE
        self.carried_calls = carried_calls
        self.projected_arrivals = projected_arrivals
        self.arrival_rate_per_day = arrival_rate_per_day

    @property
    def is_projected(self) -> bool:
        return self.basis == "PROJECTED"

    def to_dict(self) -> dict:
        out: dict[str, Any] = {
            "requested": self.requested,
            "basis": self.basis,
            "measured_through": self.measured_through,
        }
        if self.is_projected:
            out.update({
                "carried_calls": self.carried_calls,
                "projected_arrivals": self.projected_arrivals,
                "arrival_rate_per_day": self.arrival_rate_per_day,
                "assumption_id": PROJECTION_ASSUMPTION_ID,
            })
        return out


def _op_end(call: dict) -> Optional[datetime]:
    for key in ("cargo_operation_end", "departure_time"):
        if call.get(key) is not None:
            return call[key]
    return None


def _op_start(call: dict) -> Optional[datetime]:
    for key in ("cargo_operation_start", "berthing_time", "ata", "eta"):
        if call.get(key) is not None:
            return call[key]
    return None


def still_working_at(calls: list[dict], boundary: datetime) -> list[dict]:
    """Calls whose operation window is still open at ``boundary``.

    A call with a start but no recorded end counts as still working: an unclosed
    window means the operation had not finished when the report was cut, which is
    precisely the state that carries into the next day."""
    out = []
    for call in calls:
        start = _op_start(call)
        if start is None or start > boundary:
            continue
        end = _op_end(call)
        if end is None or end > boundary:
            out.append(call)
    return out


def mean_daily_arrivals(calls: list[dict]) -> Optional[float]:
    """Measured mean arrivals per day over the span the calls cover.

    Derived, not assumed — this is the rate the port actually ran at over the
    corpus window. Returns None when the span is too short to divide."""
    starts = [s for s in (_op_start(c) for c in calls) if s is not None]
    if len(starts) < 2:
        return None
    span_days = (max(starts) - min(starts)).total_seconds() / 86400.0
    if span_days < 1:
        return None
    return round(len(starts) / span_days, 2)


async def load_calls_for(
    repo: Any, res: SimulationResult, *,
    as_of: datetime, horizon_hours: int, terminal: Optional[str] = None,
) -> tuple[list[dict], Coverage]:
    """Calls for the requested window, projected forward when it is empty.

    Appends its own query traces and assumptions to ``res`` so the caller does not
    have to remember to. Returns ``(calls, coverage)``; an empty list with
    ``basis == "NONE"`` means there is genuinely nothing to stand on and the
    caller should block the answer.
    """
    to_ts = as_of + timedelta(hours=horizon_hours)
    rows, trace = await repo.calls_with_moves(terminal=terminal, from_ts=as_of,
                                              to_ts=to_ts)
    res.trace(trace)
    if rows:
        return list(rows), Coverage(requested=as_of, measured_through=as_of,
                                    basis="MEASURED")

    # Nothing in the requested window. Look back for the last day that has data.
    lookback_from = as_of - timedelta(days=LOOKBACK_DAYS)
    history, history_trace = await repo.calls_with_moves(
        terminal=terminal, from_ts=lookback_from, to_ts=as_of)
    res.trace(history_trace)
    if not history:
        res.note(
            f"no calls in the requested window and none in the {LOOKBACK_DAYS} days "
            f"before {as_of.date().isoformat()} — there is nothing to project from, "
            "so no figure is produced.", blocks_answer=True)
        return [], Coverage(requested=as_of, basis="NONE")

    history = list(history)
    last_start = max(s for s in (_op_start(c) for c in history) if s is not None)
    measured_through = last_start

    carried = still_working_at(history, as_of)
    rate = mean_daily_arrivals(history)

    for call in carried:
        call["data_origin"] = "PROJECTED"

    coverage = Coverage(
        requested=as_of, measured_through=measured_through, basis="PROJECTED",
        carried_calls=len(carried),
        projected_arrivals=int(round(rate)) if rate else 0,
        arrival_rate_per_day=rate)

    res.assume(
        "projection_basis",
        f"carried forward from {measured_through.date().isoformat()}",
        f"[{PROJECTION_ASSUMPTION_ID}] {as_of.date().isoformat()} lies beyond the "
        f"data (ground truth ends {measured_through.date().isoformat()}; bidder API "
        "access closed 7 Aug 2026 1700). The state is the calls still working at "
        "the end of the last measured day; no vessel name, voyage or move count is "
        "invented for it.",
        SOURCE_DERIVED)
    if rate:
        res.assume(
            "arrival_rate_per_day", rate,
            "mean daily arrivals measured over the corpus window, used to size the "
            "additional demand on the projected day. Arrivals enter as counted "
            "placeholders, not as named vessels.",
            SOURCE_MEASURED)
    res.note(
        f"{as_of.date().isoformat()} is a PROJECTED day: {len(carried)} call(s) "
        f"carried forward from {measured_through.date().isoformat()}"
        + (f" plus ~{int(round(rate))} expected arrivals at the measured rate."
           if rate else ".")
        + " Figures below are projections, not measurements.")
    return carried, coverage
