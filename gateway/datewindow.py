"""One definition of "a date window", for every router that lists records.

WHY THIS IS SHARED. A survey on 17-Aug-2026 found 83 paginated list endpoints
with no date filter at all, while the handful that had one each re-derived the
semantics locally. Two of those semantics are easy to get wrong and impossible
to spot from the response:

  * **Inclusivity.** `to_date` is INCLUSIVE of the whole day. Comparing
    ``ts <= to_date`` silently drops everything after midnight on the last day,
    so a one-day search returns nothing and looks like missing data. The bound
    is therefore half-open on `to_date + 1 day`.
  * **Time zone.** JNPA records are stamped in IST — the gate slips print local
    wall-clock, the berthing reports are local, and the API's own envelope is
    `Asia/Kolkata`. Anchoring a date filter in UTC shifts every boundary by 5h30,
    so a "06 June" search quietly loses the first 5.5 hours of the 6th and gains
    the last 5.5 of the 5th. Boundaries are built in IST.

Both mistakes return a plausible, wrong answer rather than an error, which is
precisely the failure mode the JNPA Notice's "state the method" clause is meant
to catch. Hence one implementation, imported everywhere.

Usage in a router::

    from ..datewindow import DateWindow, date_window

    @router.get("/things")
    async def list_things(window: DateWindow = Depends(date_window), ...):
        rows = await svc.list(window=window, ...)

and in the repository::

    clause, params = window.sql("created_at")
    sql = f"SELECT ... FROM t WHERE 1=1 {clause}"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException, Query

#: JNPA operates in IST and every timestamp in the corpus is local wall-clock.
IST = timezone(timedelta(hours=5, minutes=30))

#: Longest window a single request may span. The corpus itself covers roughly
#: Feb-Aug 2026, so a quarter is generous for any real question while still
#: stopping an unbounded scan of a 480k-row table.
MAX_WINDOW_DAYS = 92


@dataclass(frozen=True)
class DateWindow:
    """An inclusive `[from_date, to_date]` day range, or either half, or neither.

    `None` on both sides means "no window" and MUST produce no SQL at all, so
    that adding this dependency to an existing endpoint cannot change the result
    of a call that does not use it.
    """

    from_date: Optional[date] = None
    to_date: Optional[date] = None

    @property
    def is_open(self) -> bool:
        """True when neither bound is set — i.e. the filter is inert."""
        return self.from_date is None and self.to_date is None

    def start_ts(self) -> Optional[datetime]:
        """Inclusive lower bound as an IST instant."""
        if self.from_date is None:
            return None
        return datetime.combine(self.from_date, time.min, tzinfo=IST)

    def end_ts(self) -> Optional[datetime]:
        """EXCLUSIVE upper bound: midnight IST at the START of the next day, so
        the whole of `to_date` is included."""
        if self.to_date is None:
            return None
        return datetime.combine(self.to_date + timedelta(days=1), time.min, tzinfo=IST)

    def end_ts_inclusive(self) -> Optional[datetime]:
        """Upper bound for a query that compares with ``<=`` rather than ``<``.

        Half-open (`< next midnight`) is the correct shape and what :meth:`sql`
        emits. Some older repositories predate this module and compare
        ``col <= :bound``; handing those an exclusive bound silently admits a
        record stamped exactly 00:00:00 on the following day. This returns the
        last representable instant INSIDE `to_date` so those call sites stay
        correct without rewriting their SQL.
        """
        end = self.end_ts()
        return None if end is None else end - timedelta(microseconds=1)

    def sql(self, column: str, *, prefix: str = "dw") -> Tuple[str, Dict[str, Any]]:
        """Build ``AND col >= :dw_from AND col < :dw_to_excl`` for `column`.

        Returns ``("", {})`` when the window is open, so a caller can splice the
        result into any WHERE chain unconditionally.

        `column` is a SQL identifier supplied by the CALLER, never by a client —
        the parameter names are fixed and the values are always bound.
        """
        clauses: list[str] = []
        params: Dict[str, Any] = {}
        start, end = self.start_ts(), self.end_ts()
        if start is not None:
            clauses.append(f"{column} >= :{prefix}_from")
            params[f"{prefix}_from"] = start
        if end is not None:
            clauses.append(f"{column} < :{prefix}_to_excl")
            params[f"{prefix}_to_excl"] = end
        return ((" AND " + " AND ".join(clauses)) if clauses else ""), params

    def describe(self) -> Optional[str]:
        """Human-readable window for a response envelope / evidence trail."""
        if self.is_open:
            return None
        lo = self.from_date.isoformat() if self.from_date else "…"
        hi = self.to_date.isoformat() if self.to_date else "…"
        return f"{lo} to {hi} (IST, inclusive)"


def validate_window(from_date: Optional[date], to_date: Optional[date]) -> None:
    """400 on an inverted or oversized range. Shared by the dependency and by
    routers that still parse their own dates."""
    if from_date and to_date:
        if to_date < from_date:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_window",
                        "message": "to_date must not precede from_date"},
            )
        if (to_date - from_date).days > MAX_WINDOW_DAYS:
            raise HTTPException(
                status_code=400,
                detail={"error": "invalid_window",
                        "message": f"window must not exceed {MAX_WINDOW_DAYS} days"},
            )


def date_window(
    from_date: Optional[date] = Query(
        None,
        description="Window start, inclusive (YYYY-MM-DD, IST). Omit for no lower bound.",
    ),
    to_date: Optional[date] = Query(
        None,
        description="Window end, INCLUSIVE of the whole day (YYYY-MM-DD, IST). "
                    "Omit for no upper bound.",
    ),
) -> DateWindow:
    """FastAPI dependency: parse and validate `?from_date=&to_date=`."""
    validate_window(from_date, to_date)
    return DateWindow(from_date=from_date, to_date=to_date)


#: Timestamp column names, in the order we prefer them when a table offers
#: several. An OPERATIONAL time (when the thing happened) beats an INGEST time
#: (when we heard about it): a June trace filtered on `created_at` returns
#: nothing, because every corpus row was loaded in August.
_PREFERRED_TS_ORDER = (
    # 1. When the event happened.
    "event_ts", "first_event_ts", "ts", "doc_ts", "gate_pass_ts", "issued_at", "placed_at",
    "truck_in_time", "shipping_ts", "arrival_ts", "assigned_at",
    "occurred_at", "detected_at", "captured_at", "started_at",
    # 2. The date the DOCUMENT carries. A shipping bill is dated `sb_date`;
    #    filtering it on `created_at` filters on when we loaded the file, which
    #    for this corpus is August for every row — so a June query returns
    #    nothing and reads as missing data.
    "report_date", "report_month", "do_date", "leo_date", "sb_date", "igm_date",
    "receipt_date", "eta",
    # 3. Ingest time, last: it answers "when did we hear about this", which is
    #    almost never the question, but it is better than no window at all.
    "created_at", "updated_at",
)


def preferred_ts_column(available: "set[str] | frozenset[str]") -> Optional[str]:
    """Pick the timestamp a date window should apply to.

    Returns None when the table has none — which is an answer, and the caller
    must say so rather than filter on something arbitrary.
    """
    for col in _PREFERRED_TS_ORDER:
        if col in available:
            return col
    return None


def apply_window(where: str, window: "DateWindow", column: str,
                 params: dict) -> str:
    """Splice a date window into an inline ``WHERE`` fragment.

    `where` is either ``""`` or a full ``"WHERE ..."`` clause, which is the
    shape most routers in this gateway build by hand. Returns the new clause and
    mutates `params` with the bound window values.

    `column` is supplied by the CALLER and is always a fixed identifier; only the
    bounds are bound parameters.
    """
    if window is None or window.is_open:
        return where
    frag, wparams = window.sql(column)
    params.update(wparams)
    cond = frag.removeprefix(" AND ").strip()
    if not cond:
        return where
    return f"{where} AND {cond}" if where.strip() else f"WHERE {cond}"


#: Reserved keys for carrying a window inside a `filters` mapping.
#:
#: They are underscore-prefixed on purpose. Several repositories accept a
#: `filters` dict and read only whitelisted keys, but not every consumer does —
#: a test double in tests/test_customs_api.py compared EVERY key in `filters`
#: against the row, so plain `window` / `date_col` keys were read as column
#: filters and matched nothing. Underscore marks them as control values rather
#: than data, and any generic consumer can skip keys starting with "_".
WINDOW_KEY = "_window"
DATE_COL_KEY = "_date_col"


def window_cond(window: "DateWindow", column: str, params: dict) -> Optional[str]:
    """The window as a bare condition, for repositories that collect a LIST of
    conditions and join them with AND (the dominant shape in ``services/*``).

    Returns None when the window is open, so a caller can do::

        cond = window_cond(window, "event_ts", params)
        if cond:
            where.append(cond)

    Mutates `params` with the bound bounds. As with `apply_window`, `column` is a
    caller-supplied identifier and only the bounds are bound.
    """
    if window is None or window.is_open:
        return None
    frag, wparams = window.sql(column)
    cond = frag.removeprefix(" AND ").strip()
    if not cond:
        return None
    params.update(wparams)
    return cond
