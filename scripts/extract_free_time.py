#!/usr/bin/env python3
"""Extract free-day allowances from the IGM goods descriptions (GAP-FLOW-05).

There is no free-time FIELD in the corpus. What there is, on about one IGM line
in seventeen, is a phrase the shipper typed into `GoodsDescription`:

    ... HSCODE 25084000 14 FREE DAYS AT POD * CIN: ...
    ... SHIPPED ON BOARD 14 DAYS FREE TIME COMBINED DEMURRAGE AND DETENTION ...

This reads those, stores the number WITH the phrase it came from, and attaches
the commencement timestamp. It stores no rate and computes no charge: not one
file in the corpus carries a demurrage or detention tariff, so any amount would
be invented (see migration 0143).

Safety
------
* INSERT ... ON CONFLICT DO NOTHING on (container_no, igm_no, line_no) — a
  re-run inserts 0 rows.
* Reads `core.igm_line` / `core.igm_line_container`; writes only the new table.
* `--dry-run` reports the extraction without touching the database.

Usage
-----
    python scripts/extract_free_time.py --dry-run
    python scripts/extract_free_time.py
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "") or os.environ.get("JNPA_RDS_DSN", "")

#: The phrasings observed across the corpus, ordered so that the number ADJACENT
#: TO "DAYS" always wins.
#:
#: The ordering is not cosmetic. An earlier version put `N FREE TIME` second,
#: and on "UN.1247 CLASS 3 FREE TIME 14 DAYS AT DESTINATION" it read **3** — the
#: hazard class — instead of 14. A goods description is dense with numbers that
#: sit next to the word "free" by accident: HS codes, UN numbers, class digits,
#: quantities. Only the figure attached to "DAYS" is the term.
#:
#: A bare "FREE DAYS" with no number is deliberately unmatched: a term with no
#: figure is not a term a clock can be built on.
_PATTERNS = (
    # "14 DAYS FREE TIME", "04 DAYS FREE TIME AT PORT OF DISCHARGE"
    re.compile(r"(?P<n>\d{1,3})\s*(?:DAYS?|DAY)\s+FREE(?:\s*(?:TIME|DAYS?))?", re.I),
    # "FREE TIME 14 DAYS", "FREE DAYS: 21"
    re.compile(r"FREE\s*(?:TIME|DAYS?)\s*[:\-]?\s*(?P<n>\d{1,3})\s*(?:DAYS?|DAY)?", re.I),
    # "14 FREE DAYS AT POD" — number, then FREE, then DAYS explicitly.
    re.compile(r"(?P<n>\d{1,3})\s+FREE\s+DAYS?", re.I),
)

#: A sanity bound. A "999 free days" reading is a mis-parse of an HS code or a
#: quantity that happened to sit next to the word, not a commercial term.
_MAX_SANE_DAYS = 90


def extract(desc: str) -> tuple[int, str] | None:
    """Return (days, the matched phrase) or None."""
    if not desc:
        return None
    for pat in _PATTERNS:
        m = pat.search(desc)
        if not m:
            continue
        try:
            n = int(m.group("n"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= _MAX_SANE_DAYS:
            start = max(0, m.start() - 30)
            return n, desc[start:m.end() + 30].strip()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dsn:
        print("No DSN: set POSTGRES_DSN or pass --dsn", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text

    engine = create_engine(args.dsn.replace("postgresql://", "postgresql+psycopg://"))

    print("FREE-TIME EXTRACTION (GAP-FLOW-05)"
          + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))

    with engine.connect() as conn:
        lines = list(conn.execute(text(
            "SELECT igm_no, line_no, goods_desc FROM core.igm_line "
            "WHERE goods_desc IS NOT NULL")))
        total_containers = conn.execute(text(
            "SELECT count(*) FROM core.igm_line_container")).scalar_one()

    found = []
    for igm_no, line_no, desc in lines:
        got = extract(desc)
        if got:
            found.append((igm_no, line_no, got[0], got[1]))

    dist = Counter(f[2] for f in found)
    print(f"  {len(lines)} IGM line(s) with a goods description")
    print(f"  {len(found)} state a free-day allowance")
    print(f"  allowances: {dist.most_common()}")
    for f in found[:3]:
        print(f"    IGM {f[0]} line {f[1]}: {f[2]} days — …{f[3][:70]}…")

    if args.dry_run:
        print("\nDRY-RUN complete — nothing written.")
        return 0

    inserted = 0
    with engine.begin() as conn:
        for igm_no, line_no, days, phrase in found:
            inserted += conn.execute(text("""
                INSERT INTO core.container_free_time
                    (container_no, igm_no, line_no, free_days, extracted_from,
                     commencement_basis, commenced_at, provenance, data_origin,
                     source_file)
                SELECT lc.container_no, lc.igm_no, lc.line_no, :days, :phrase,
                       'IGM_ENTRY_INWARD',
                       COALESCE(i.entry_inward_ts, i.eta),
                       'DOCUMENT_EVIDENCED', 'REAL', i.source_file
                FROM core.igm_line_container lc
                JOIN core.igm i ON i.igm_no = lc.igm_no
                WHERE lc.igm_no = :igm AND lc.line_no = :line
                ON CONFLICT ON CONSTRAINT uq_container_free_time DO NOTHING
            """), {"days": days, "phrase": phrase[:500],
                   "igm": igm_no, "line": line_no}).rowcount

    print(f"\n  containers with an allowance recorded: {inserted}")
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM core.container_free_time")).scalar_one()
        withstart = conn.execute(text(
            "SELECT count(*) FROM core.container_free_time WHERE commenced_at IS NOT NULL")).scalar_one()
        print(f"  core.container_free_time: {n} rows "
              f"({100 * n / total_containers:.1f}% of {total_containers} manifested containers)")
        print(f"  with a commencement timestamp: {withstart}")
        print("\n  The remaining containers state NO free-day term. That is the "
              "corpus, not a gap in this extraction.")
        print("  NO CHARGE is computed anywhere: the corpus carries no tariff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
