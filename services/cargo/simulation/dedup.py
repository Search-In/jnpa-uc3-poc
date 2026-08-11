"""Duplicate collapse for the JNPA daily feeds.

Two of the source feeds repeat the same real-world object across consecutive
daily files. Counting *rows* instead of *objects* silently multiplies every
headline figure, and the multiplier is large enough to change an answer rather
than merely blur it:

======================  ===========  ==============  ============
feed                    raw rows     distinct        factor
======================  ===========  ==============  ============
daily berthing reports  145          70 calls        **2.07x**
FOIS train intimation   1,468        233 rakes       **6.3x**
======================  ===========  ==============  ============

(measured over the corpus window 11 Jul - 5 Aug 2026 for berthing, 1 - 25 Jul
2026 for rail; pinned in ``tests/test_golden_figures.py``)

Why the repetition happens
--------------------------
* **Berthing.** A daily berthing report lists the vessels *alongside on that
  day*. A call that spans midnight is therefore reported again the next day, and
  once more the day after if it is still working. 57 of the 70 real calls in the
  corpus appear on more than one report day.
* **Rail.** A train intimation file is a *snapshot of rakes in transit toward
  JNPT*, not a list of arrivals. The same rake reappears every day until it
  arrives — one rake in the corpus appears in 15 consecutive files — with its
  ``Last Reporting Station`` advancing each time.

The rail case is the more dangerous of the two, because the naive row count
(~54 rakes/day) is close enough to plausible that nothing looks wrong. The
distinct count by expected arrival date is ~40/day.

Verified property of the current corpus
---------------------------------------
Every one of the 57 duplicated berthing groups is **byte-identical** across the
report days it appears on — no field differs. Collapsing therefore loses
nothing today. :func:`distinct_calls` still prefers the most complete row rather
than simply the first, so that a live feed which *does* fill in ``sailed`` on a
later report keeps the richer row instead of the earlier stub.

These are pure functions over dicts: no database, no I/O. The ingest applies
them at write time so the duplicates never reach a table, and the tests apply
them to the raw corpus so the arithmetic is pinned independently of any
database state.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "call_key",
    "distinct_calls",
    "rake_key",
    "rake_arrival_date",
    "rakes_by_expected_arrival",
    "duplication_factor",
]


def _get(row: Mapping[str, Any], *names: str) -> Any:
    """First present, non-empty value among ``names``.

    Callers arrive from two directions with two naming conventions: the JNPA API
    corpus is camelCase (``vesselName``, ``voyage``, ``alongside``) and the
    database is snake_case (``vessel_name``, ``voyage_number``, ``berthing_time``).
    Accepting both keeps one implementation for the ingest and the tests rather
    than two that can drift apart."""
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _norm(value: Any) -> str:
    """Case- and whitespace-insensitive form for identity comparison.

    Vessel names arrive with inconsistent internal spacing across terminals
    ("MSC  JASMINE X"), which would otherwise split one call into two."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().upper()


# ------------------------------------------------------------------ berthing
def call_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """Identity of one vessel call: ``(vessel_name, voyage, alongside)``.

    ``alongside`` is part of the key deliberately. A vessel/voyage pair is not
    unique on its own — the same voyage designator recurs on later rotations —
    whereas the moment the vessel came alongside pins the individual call. It is
    also the field the daily reports repeat verbatim, which is what makes the
    collapse exact rather than fuzzy."""
    return (
        _norm(_get(row, "vesselName", "vessel_name")),
        _norm(_get(row, "voyage", "voyage_number")),
        _norm(_get(row, "alongside", "berthing_time")),
    )


def _completeness(row: Mapping[str, Any]) -> int:
    """How many of the fields that later reports tend to fill are present.

    Used only to break ties between repeated rows. Higher wins."""
    fields = (
        ("sailed", "departure_time"),
        ("operationsEnd", "cargo_operation_end"),
        ("operationsStart", "cargo_operation_start"),
        ("totalMoves", "gross_moves"),
        ("importMoves", "discharge_moves"),
        ("exportMoves", "load_moves"),
        ("berth", "berth_number"),
    )
    return sum(1 for names in fields if _get(row, *names) is not None)


def distinct_calls(rows: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Collapse repeated berthing rows to one row per real vessel call.

    Where a call appears on several report days the most complete row wins; ties
    keep the first seen, so the result is deterministic for a given input order.
    Rows with no usable identity (no vessel name *and* no alongside time) are
    dropped rather than merged under an empty key — they cannot be attributed to
    a call, and inventing an identity for them would be worse than losing them.

    Input order is otherwise preserved, so a caller that sorted by report date
    still gets chronological output."""
    best: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows:
        key = call_key(row)
        if not key[0] and not key[2]:
            continue
        current = best.get(key)
        if current is None:
            best[key] = dict(row)
            order.append(key)
        elif _completeness(row) > _completeness(current):
            best[key] = dict(row)
    return [best[k] for k in order]


# ---------------------------------------------------------------------- rail
def rake_key(row: Mapping[str, Any]) -> str:
    """Identity of one rake: the FOIS ``RakeId``.

    ``RakeName`` is not unique (it is a human label such as ``GL-22-``) and the
    reporting station changes daily by design, so ``RakeId`` is the only stable
    identity in the intimation file."""
    return _norm(_get(row, "RakeId", "rake_id", "rakeId"))


def rake_arrival_date(row: Mapping[str, Any]) -> Optional[str]:
    """Expected arrival date as ``YYYY-MM-DD`` from the FOIS ``Eda`` field.

    ``Eda`` is formatted ``DDMMYYYY:HH:MM``. This is the field that answers
    "how many rakes arrive on day D"; the *file* date only answers "when was the
    snapshot taken", which is why counting files or rows overstates arrivals."""
    raw = _get(row, "Eda", "eda", "expected_arrival")
    if raw is None:
        return None
    match = re.match(r"^\s*(\d{2})(\d{2})(\d{4})", str(raw))
    if not match:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def rakes_by_expected_arrival(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Map expected arrival date -> set of distinct ``RakeId``.

    This is the defensible basis for a daily rail evacuation figure. Rows with no
    ``RakeId`` or no parseable ``Eda`` are skipped: a rake that cannot be
    identified cannot be counted once, and one with no expected arrival cannot be
    attributed to a day."""
    by_date: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        rake = rake_key(row)
        arrival = rake_arrival_date(row)
        if rake and arrival:
            by_date[arrival].add(rake)
    return dict(by_date)


# --------------------------------------------------------------------- shared
def duplication_factor(raw_count: int, distinct_count: int) -> float:
    """``raw / distinct``, or 0.0 when there is nothing to divide.

    Surfaced in the ingest summary so an operator sees how much the source feed
    repeated itself, rather than having to infer it from two row counts."""
    if not distinct_count:
        return 0.0
    return round(raw_count / distinct_count, 2)
