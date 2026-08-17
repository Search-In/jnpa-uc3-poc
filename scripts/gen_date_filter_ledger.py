#!/usr/bin/env python3
"""Regenerate 08_DATE_FILTER_COVERAGE.md from the routers.  GAP-DATE-01.

The hand-written ledger drifted in both directions: it listed endpoints as
"remaining" that already carried their own date parameters under different
names (`from`/`to`, `date_from`/`date_to`), and it counted surfaces that are not
list endpoints at all — a health check, an upload template, a single-object
media proxy — where a date window has no meaning.

A ledger that is wrong about the code is worse than no ledger: it sends the next
person to add a parameter that already exists, and it inflates the outstanding
count. So it is generated, not maintained.

    python scripts/gen_date_filter_ledger.py            # print
    python scripts/gen_date_filter_ledger.py --write    # update the doc
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gateway.date_window_policy import EXCLUSIONS, excluded  # noqa: E402
ROUTERS = ROOT / "gateway" / "routers"
DOC = ROOT.parent / "audit" / "corpus_thread_2026-08-16" / "08_DATE_FILTER_COVERAGE.md"

#: A date parameter under any of the names in use across this gateway.
OWN_DATE = re.compile(
    r"\b(date_from|date_to|since|start_date|end_date|as_of|day|for_date|on_date)\b"
    r"|alias=[\"'](from|to)[\"']")

GET = re.compile(r"@router\.get\(\s*[\"']([^\"']*)[\"']")


def classify(body: str, router: str = "", func: str = "") -> str:
    sig = body.split(") ->")[0]
    if "date_window" in body:
        return "shared"
    if OWN_DATE.search(sig):
        return "own"
    # An exemption is a RECORDED DECISION, not a silent omission — see
    # gateway/date_window_policy.py. Anything NOT listed there counts as
    # outstanding, so a new endpoint is treated as needing a window until
    # someone deliberately says otherwise.
    if excluded(router, func):
        return "not_applicable"
    return "remaining"


def is_list_surface(path: str, body: str) -> bool:
    """A paginated collection read — the only thing a date window applies to.

    Detail-by-id routes are excluded (a window on a single record is
    meaningless), and so are the aggregate/health/template surfaces that expose
    no collection to bound.
    """
    if "{" in path:                       # /thing/{id} — one record
        return False
    sig = body.split(") ->")[0]
    return "limit" in sig or "offset" in sig


def scan() -> list[tuple[str, str, str, str]]:
    out = []
    for f in sorted(ROUTERS.glob("*.py")):
        src = f.read_text()
        for m in GET.finditer(src):
            path = m.group(1) or "(root)"
            fm = re.search(r"async def (\w+)\(", src[m.end():])
            if not fm:
                continue
            start = m.end() + fm.start()
            nd = re.search(r"\n(?:@router|async def |def )", src[start + 10:])
            body = src[start: start + 10 + (nd.start() if nd else 4000)]
            if not is_list_surface(path, body):
                continue
            out.append((f.name, path, fm.group(1), classify(body, f.name, fm.group(1))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    rows = scan()
    n = Counter(r[3] for r in rows)
    lines = [
        "# 08 · Date-filter coverage — every list endpoint",
        "",
        "**Generated** by `scripts/gen_date_filter_ledger.py`. Do not hand-edit: the",
        "hand-maintained version drifted in both directions, listing endpoints as",
        "outstanding that already carried a date parameter under a different name, and",
        "counting surfaces where a window has no meaning.",
        "",
        f"**{len(rows)} paginated list endpoints — {n['shared']} on the shared "
        f"primitive · {n['own']} with their own date parameter · "
        f"{n['not_applicable']} N/A by recorded decision · "
        f"{n['remaining']} remaining**",
        "",
        "Detail-by-id routes and aggregate/health/template surfaces are excluded: a",
        "date window on a single record, or on a health check, has no meaning.",
        "",
        "## Why this is one shared primitive",
        "",
        "`gateway/datewindow.py` owns the semantics, and three of them return a",
        "plausible wrong answer rather than an error:",
        "",
        "* **`to_date` is inclusive of the whole day.** A `ts <= to_date` comparison",
        "  drops everything after midnight on the final day, so a one-day query returns",
        "  nothing and reads as missing data.",
        "* **Bounds are anchored in IST.** Every JNPA record is local wall-clock; a UTC",
        "  anchor loses the first 5.5 hours of the requested day.",
        "* **The column must be the OPERATIONAL one.** Filtering on `created_at` filters",
        "  on when we loaded the file — which for this corpus is August for every row —",
        "  so a June query returns nothing. `preferred_ts_column()` ranks document and",
        "  event times ahead of ingest times for exactly this reason.",
        "",
        "The window travels to shared where-builders under the reserved keys `_window`",
        "and `_date_col` (`gateway.datewindow.WINDOW_KEY`). The underscore is load-",
        "bearing: a consumer that compares every key in a `filters` mapping against the",
        "row would otherwise read them as column filters and match nothing.",
        "",
    ]
    titles = {"shared": "On the shared primitive ✅",
              "own": "Carry their own date parameter ✅",
              "not_applicable": "Not applicable — recorded decision, with the ground",
              "remaining": "Remaining ○"}
    for key in ("shared", "own", "not_applicable", "remaining"):
        sel = [r for r in rows if r[3] == key]
        if key == "not_applicable":
            lines += [f"## {titles[key]} ({len(sel)})", "",
                      "Enumerated in `gateway/date_window_policy.py` and reviewed by a",
                      "person, because a heuristic got several of these wrong in both",
                      "directions. A window that filters nothing useful is worse than an",
                      "absent one: it looks like it works.",
                      "",
                      "| Router | Function | Ground | Why |", "|---|---|---|---|"]
            for r in sel:
                ground, why = excluded(r[0], r[2])
                lines.append(f"| `{r[0]}` | `{r[2]}` | {ground} | {why} |")
        else:
            lines += [f"## {titles[key]} ({len(sel)})", "",
                      "| Router | Path | Function |", "|---|---|---|"]
            lines += [f"| `{r[0]}` | `{r[1]}` | `{r[2]}` |" for r in sel]
        lines.append("")

    text = "\n".join(lines)
    if args.write:
        DOC.write_text(text)
        print(f"wrote {DOC}")
    print(f"{len(rows)} endpoints: " + ", ".join(f"{k}={v}" for k, v in sorted(n.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
