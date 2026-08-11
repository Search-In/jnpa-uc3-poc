"""Golden figures — the arithmetic the JNPA what-if answers rest on.

Every number below was computed independently from the JNPA API corpus and is
pinned here so that a regression fails in CI rather than in front of an
evaluator. These are not smoke tests: each assertion is a figure that appears,
directly or one step removed, in a submitted answer.

Two layers, deliberately:

* **Unit** (always run) — the collapse rules in
  :mod:`services.cargo.simulation.dedup`, on hand-built rows. No corpus, no
  database, so these guard the logic even on a bare checkout.
* **Corpus** (run when the corpus is present) — the real figures. Skipped with a
  clear message when the corpus is not on disk, so a CI box without it stays
  green instead of failing for the wrong reason.

Point the corpus tests at a different replay with ``JNPA_CORPUS_DIR``; the
default is the sibling ``jnpa-mock-server`` checkout.

Why these particular numbers
----------------------------
The two source feeds repeat themselves (see :mod:`dedup`), and the inflation is
large enough to change an answer:

* berthing 145 raw rows -> **70** real calls (2.07x)
* rail 1,468 raw rows -> **233** real rakes (6.3x)

The median berth-hour productivity is the clearest illustration of why this
matters: **58.16** moves/hour on the 70 distinct calls, against 57.3 if the
duplicated rows are counted. Close enough to look right, wrong enough to be
wrong. Same trap, larger, on the rail side: ~54 rakes/day by row count against
~39.8 by distinct rake.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.cargo.simulation.dedup import (  # noqa: E402
    call_key,
    distinct_calls,
    duplication_factor,
    rake_arrival_date,
    rake_key,
    rakes_by_expected_arrival,
)
from services.cargo.simulation.gate_slotting import percentile  # noqa: E402


# --------------------------------------------------------------------- corpus
def _corpus_root() -> Path:
    return Path(os.environ.get(
        "JNPA_CORPUS_DIR", str(REPO_ROOT.parent / "jnpa-mock-server"))).resolve()


def _require_corpus() -> Path:
    root = _corpus_root()
    if not (root / "data" / "responses").is_dir():
        pytest.skip(
            f"JNPA corpus not found at {root}. Set JNPA_CORPUS_DIR to the "
            "jnpa-mock-server checkout to run the corpus-backed golden figures.")
    return root


def _berthing_rows() -> list[dict]:
    """Every vesselCalls row across every daily berthing report, un-deduplicated."""
    path = _require_corpus() / "data" / "responses" / "group-berthing-reports.json"
    payload = json.loads(path.read_text(encoding="utf8"))
    rows: list[dict] = []
    for report in payload.get("items", []):
        for call in report.get("vesselCalls", []) or []:
            rows.append({"reportDate": report.get("reportDate", "")[:10],
                         "terminal": report.get("terminal"), **call})
    return rows


def _fois_rows() -> list[dict]:
    """Every rake row across every train-intimation snapshot whose filename date
    falls in July 2026 (the month the rail feed actually covers)."""
    root = _require_corpus()
    rows: list[dict] = []
    pattern = str(root / "data" / "files" / "ref_*" / "JNPA Train Intimation *.csv")
    for path in glob.glob(pattern):
        match = re.search(r"Intimation (\d{2})(\d{2})(\d{4})", path)
        if not match:
            continue
        day, month, year = match.groups()
        if f"{year}-{month}" != "2026-07":
            continue
        with open(path, encoding="utf8", errors="replace") as handle:
            rows.extend(csv.DictReader(handle))
    return rows


def _hours(start: str, end: str) -> float:
    fmt = lambda s: datetime.fromisoformat(str(s))  # noqa: E731
    return (fmt(end) - fmt(start)).total_seconds() / 3600.0


def _gmph_series(calls: list[dict]) -> list[float]:
    """Berth-hour gross moves per hour worked, one per call, ascending."""
    out = []
    for call in calls:
        start, end, moves = (call.get("operationsStart"), call.get("operationsEnd"),
                             call.get("totalMoves"))
        if not (start and end and moves):
            continue
        worked = _hours(start, end)
        if worked > 0:
            out.append(round(moves / worked, 2))
    return sorted(out)


# ============================================================ unit: berthing
def test_call_key_ignores_case_and_internal_whitespace():
    """One call reported with inconsistent spacing must not split into two."""
    a = {"vesselName": "MSC  JASMINE X", "voyage": "abc1", "alongside": "2026-08-01T10:00:00+05:30"}
    b = {"vesselName": "msc jasmine x", "voyage": "ABC1", "alongside": "2026-08-01T10:00:00+05:30"}
    assert call_key(a) == call_key(b)


def test_call_key_accepts_both_naming_conventions():
    """The corpus is camelCase and the database is snake_case; one key for both."""
    camel = {"vesselName": "ARTAM", "voyage": "V1", "alongside": "2026-07-11T08:00:00+05:30"}
    snake = {"vessel_name": "ARTAM", "voyage_number": "V1",
             "berthing_time": "2026-07-11T08:00:00+05:30"}
    assert call_key(camel) == call_key(snake)


def test_distinct_calls_collapses_repeats_and_keeps_order():
    rows = [
        {"vesselName": "A", "voyage": "1", "alongside": "T0", "reportDate": "2026-08-01"},
        {"vesselName": "B", "voyage": "2", "alongside": "T1", "reportDate": "2026-08-01"},
        {"vesselName": "A", "voyage": "1", "alongside": "T0", "reportDate": "2026-08-02"},
    ]
    out = distinct_calls(rows)
    assert [r["vesselName"] for r in out] == ["A", "B"]


def test_distinct_calls_prefers_the_more_complete_row():
    """A later report that fills in `sailed` must win over the earlier stub."""
    stub = {"vesselName": "A", "voyage": "1", "alongside": "T0", "operationsStart": "S"}
    full = {"vesselName": "A", "voyage": "1", "alongside": "T0", "operationsStart": "S",
            "operationsEnd": "E", "sailed": "D", "totalMoves": 100}
    assert distinct_calls([stub, full])[0]["sailed"] == "D"
    assert distinct_calls([full, stub])[0]["sailed"] == "D"


def test_distinct_calls_drops_unidentifiable_rows():
    """No vessel name and no alongside time cannot be attributed to a call."""
    assert distinct_calls([{"voyage": "1"}]) == []


def test_duplication_factor_handles_empty():
    assert duplication_factor(0, 0) == 0.0
    assert duplication_factor(145, 70) == 2.07


# ================================================================ unit: rail
def test_rake_arrival_date_parses_fois_format():
    """FOIS `Eda` is DDMMYYYY:HH:MM — the arrival day, not the snapshot day."""
    assert rake_arrival_date({"Eda": "21072026:19:52"}) == "2026-07-21"


def test_rake_arrival_date_returns_none_when_unparseable():
    assert rake_arrival_date({"Eda": ""}) is None
    assert rake_arrival_date({}) is None


def test_rakes_by_expected_arrival_counts_each_rake_once_per_day():
    """The same rake seen in three snapshots is one arrival, not three."""
    rows = [
        {"RakeId": "R1", "Eda": "21072026:10:00"},
        {"RakeId": "R1", "Eda": "21072026:11:00"},
        {"RakeId": "R2", "Eda": "21072026:12:00"},
        {"RakeId": "R3", "Eda": "22072026:09:00"},
    ]
    by_date = rakes_by_expected_arrival(rows)
    assert by_date["2026-07-21"] == {"R1", "R2"}
    assert by_date["2026-07-22"] == {"R3"}


def test_rakes_without_identity_or_arrival_are_skipped():
    rows = [{"RakeId": "", "Eda": "21072026:10:00"}, {"RakeId": "R9", "Eda": "junk"}]
    assert rakes_by_expected_arrival(rows) == {}


def test_rake_key_normalises():
    assert rake_key({"RakeId": " srew170626121132 "}) == "SREW170626121132"


# ========================================================= corpus: berthing
def test_AC18_distinct_vessel_calls():
    """AC-18 — 145 raw rows collapse to 70 real calls; 57 span >1 report day."""
    rows = _berthing_rows()
    calls = distinct_calls(rows)
    assert len(rows) == 145, "corpus changed: raw berthing row count"
    assert len(calls) == 70, "duplicate collapse regressed"
    assert duplication_factor(len(rows), len(calls)) == 2.07

    seen: dict[tuple, int] = {}
    for row in rows:
        seen[call_key(row)] = seen.get(call_key(row), 0) + 1
    assert sum(1 for n in seen.values() if n > 1) == 57


def test_AC19_berth_hour_productivity_distribution():
    """AC-19 — gross moves per hour worked (berth), over the 70 distinct calls.

    Pinned on the DISTINCT basis. On the duplicated 145 rows the median reads
    57.3; the true figure is 58.16, and that gap is the whole reason dedup
    exists."""
    series = _gmph_series(distinct_calls(_berthing_rows()))
    assert len(series) == 70, "every distinct call must yield a productivity"
    assert series[0] == pytest.approx(23.12, abs=0.01)
    assert percentile(series, 0.25) == pytest.approx(50.55, abs=0.01)
    assert percentile(series, 0.50) == pytest.approx(58.16, abs=0.01)
    assert percentile(series, 0.75) == pytest.approx(82.87, abs=0.01)
    assert series[-1] == pytest.approx(164.91, abs=0.01)


def test_AC19_high_productivity_calls_are_multi_crane():
    """A berth-hour rate this high is several cranes on one vessel, not one crane.

    164.91 moves/hour on a 335 m call is ~4-5 cranes. 16 of the 70 calls exceed
    90/hour, so the measure must be labelled per BERTH; a per-crane figure needs
    `cranes_deployed`, which the berthing feed does not carry."""
    series = _gmph_series(distinct_calls(_berthing_rows()))
    assert sum(1 for v in series if v > 90) == 16


def test_AC20_vessels_alongside_per_day():
    """AC-20 — the bunching baseline, and the terminal imbalance the Notice names."""
    calls = distinct_calls(_berthing_rows())
    expected = {"2026-08-01": 17, "2026-08-02": 22, "2026-08-03": 17,
                "2026-08-04": 22, "2026-08-05": 21}
    for day, want in expected.items():
        start = datetime.fromisoformat(f"{day}T00:00:00+05:30")
        end = datetime.fromisoformat(f"{day}T23:59:59+05:30")
        alongside = [
            c for c in calls
            if c.get("alongside")
            and datetime.fromisoformat(c["alongside"]) <= end
            and datetime.fromisoformat(
                c.get("sailed") or c.get("operationsEnd") or c["alongside"]) >= start
        ]
        assert len(alongside) == want, f"{day}: alongside count changed"
        if day == "2026-08-05":
            bmct = sum(1 for c in alongside if c.get("terminal") == "BMCT")
            assert bmct == 12, "BMCT concentration on 5 Aug changed"


def test_corpus_coverage_stops_at_5_august():
    """The premise behind the 6 Aug projection (A-07).

    Both I-A and II-B are dated 6 Aug 2026 and the ground truth ends 5 Aug. If
    this ever fails because the corpus grew, the projection layer should be
    revisited — the scenarios could then be answered from measured data."""
    dates = {r["reportDate"] for r in _berthing_rows() if r.get("reportDate")}
    assert max(dates) == "2026-08-05"


# ============================================================= corpus: rail
def test_AC21_distinct_rakes_and_daily_arrivals():
    """AC-21 — 1,468 raw July rows are 233 real rakes (6.3x), ~39.8 arrivals/day.

    The naive row count gives ~54 rakes/day, which is plausible enough to pass
    unnoticed. It is wrong: a train intimation file is a snapshot of rakes in
    transit, so a rake in transit for a week is counted seven times."""
    rows = _fois_rows()
    assert len(rows) == 1468, "corpus changed: raw July rake row count"

    distinct = {rake_key(r) for r in rows if rake_key(r)}
    assert len(distinct) == 233
    assert duplication_factor(len(rows), len(distinct)) == 6.3

    by_date = rakes_by_expected_arrival(rows)
    window = [d for d in sorted(by_date) if "2026-07-05" <= d <= "2026-07-25"]
    mean_per_day = sum(len(by_date[d]) for d in window) / len(window)
    assert mean_per_day == pytest.approx(39.8, abs=0.1)


def test_AC21_rail_reconciles_against_the_public_reference():
    """AC-21 — the gap to the published `rakesPerMonth: 625` must be explained.

    625 rakes/month is ~20.5/day. The distinct-rake method gives ~39.8/day, about
    1.9x that; the raw row count gives ~54/day, about 2.6x. The distinct method is
    the one we publish, and the residual gap is declared as an assumption rather
    than quietly reconciled: the intimation feed counts rakes *destined for* JNPT
    from all origins, which is a wider population than rakes *handled at* the port
    terminals in the public figure.

    This test does not assert the two agree — they do not, and pretending
    otherwise would be the failure. It asserts the distinct method is materially
    closer to the reference than the row count, which is the claim we make."""
    rows = _fois_rows()
    by_date = rakes_by_expected_arrival(rows)
    window = [d for d in sorted(by_date) if "2026-07-05" <= d <= "2026-07-25"]
    distinct_per_day = sum(len(by_date[d]) for d in window) / len(window)

    public_reference_per_day = 625 / 30.4
    raw_per_day = len(rows) / 25

    assert distinct_per_day < raw_per_day
    assert abs(distinct_per_day - public_reference_per_day) < abs(
        raw_per_day - public_reference_per_day)
