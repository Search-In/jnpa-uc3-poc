#!/usr/bin/env python3
"""Import the 14 ICD daily-report PDFs (GAP-ETL-04 / GAP-ETL-07).

Reads `Data/10-Form 11_ICD Rail/ICD_REPORTS/*.pdf` through
`services.rail.parsers.icd_report_pdf` and lands two families of rows:

  * `core.icd_fpd_pendency`  — destination-wise pendency in TEUs
  * `core.icd_rake_movement` — rake placement + discharge composition

Values are stored exactly as printed. Where the report's own arithmetic does not
reconcile (20 cells, always the PDD column at NSICT or GTICT), the figure is
imported as printed and the discrepancy is echoed here — silently "fixing" a
client's published number would put a figure in the database that appears in no
JNPA document.

Safety
------
* INSERT ... ON CONFLICT DO NOTHING on a natural key, so a re-run inserts 0 rows.
* No UPDATE, no DELETE, no DDL beyond the additive migration 0141.
* `--dry-run` parses and reports without touching the database.

Usage
-----
    python scripts/import_icd_reports.py --data-dir "<corpus>/Data/10-Form 11_ICD Rail" --dry-run
    python scripts/import_icd_reports.py --data-dir "<corpus>/Data/10-Form 11_ICD Rail"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "") or os.environ.get("JNPA_RDS_DSN", "")

_PENDENCY_INSERT = """
INSERT INTO core.icd_fpd_pendency
    (report_date, terminal, series, fpd_code, teu, source_file, page_no, data_origin)
VALUES (:report_date, :terminal, :series, :fpd_code, :teu, :source_file, :page_no, 'REAL')
ON CONFLICT ON CONSTRAINT uq_icd_fpd_pendency DO NOTHING
"""

_RAKE_INSERT = """
INSERT INTO core.icd_rake_movement
    (report_date, rake_id, track, placed_at, placed_raw, discharge,
     source_file, page_no, data_origin)
VALUES (:report_date, :rake_id, :track, :placed_at, :placed_raw,
        CAST(:discharge AS jsonb), :source_file, :page_no, 'REAL')
ON CONFLICT ON CONSTRAINT uq_icd_rake_movement DO NOTHING
"""


def _resolve_placed_at(report_date: str, placed_raw: str):
    """`"30 13:10"` on a 1-July report -> 30 June 13:10.

    The report prints a day-of-month with no month. Taken naively, a rake placed
    on the 30th and reported on the 1st would be dated a month in the future. A
    day later than the report's own day therefore belongs to the previous month.
    """
    if not report_date or not placed_raw:
        return None
    try:
        rd = date.fromisoformat(report_date)
        day_s, hhmm = placed_raw.split()
        day, (hh, mm) = int(day_s), map(int, hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    month_start = rd.replace(day=1)
    if day > rd.day:
        month_start = (month_start - timedelta(days=1)).replace(day=1)
    try:
        return datetime(month_start.year, month_start.month, day, hh, mm)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dsn and not args.dry_run:
        print("No DSN: set POSTGRES_DSN or pass --dsn", file=sys.stderr)
        return 2

    from services.rail.parsers import icd_report_pdf

    pdfs = sorted(p for p in Path(args.data_dir).rglob("*.pdf") if p.is_file())
    if not pdfs:
        print(f"No PDFs under {args.data_dir}", file=sys.stderr)
        return 1

    print("ICD DAILY REPORT IMPORT (GAP-ETL-04 / GAP-ETL-07)"
          + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print(f"  {len(pdfs)} PDF(s) under {args.data_dir}\n")

    parsed, unreconciled = [], []
    tally: Counter = Counter()
    for p in pdfs:
        res = icd_report_pdf.parse(p.read_bytes(), p.name)
        if res.rejected:
            print(f"  REJECTED {p.name}: {res.reason}")
            tally["rejected"] += 1
            continue
        pend = sum(1 for r in res.rows if r["kind"] == "PENDENCY")
        rake = sum(1 for r in res.rows if r["kind"] == "RAKE")
        warn = [w for w in res.warnings
                if w["error_code"] == "pendency_does_not_reconcile"]
        unreconciled.extend((p.name, w) for w in warn)
        tally["files"] += 1
        tally["pendency"] += pend
        tally["rake"] += rake
        parsed.extend(res.rows)
        print(f"  {p.name[:46]:<48} pendency={pend:<4} rake={rake:<3}"
              f"{'  ' + str(len(warn)) + ' unreconciled' if warn else ''}")

    print(f"\n  parsed: {tally['files']} file(s), {tally['pendency']} pendency cell(s), "
          f"{tally['rake']} rake movement(s)")

    if unreconciled:
        print(f"\n  {len(unreconciled)} cell(s) where the REPORT does not reconcile "
              "(imported as printed):")
        for name, w in unreconciled[:5]:
            print(f"    {name[:34]:<36} {w['column_name']}: {w['error_detail'][:70]}")
        if len(unreconciled) > 5:
            print(f"    ... and {len(unreconciled) - 5} more")

    if args.dry_run:
        print("\nDRY-RUN complete — nothing written.")
        return 0

    from sqlalchemy import create_engine, text

    engine = create_engine(args.dsn.replace("postgresql://", "postgresql+psycopg://"))
    ins_p = ins_r = 0
    with engine.begin() as conn:
        for r in parsed:
            if r["kind"] == "PENDENCY":
                ins_p += conn.execute(text(_PENDENCY_INSERT), {
                    "report_date": r["report_date"], "terminal": r["terminal"],
                    "series": r["series"], "fpd_code": r["fpd_code"],
                    "teu": r["teu"], "source_file": r["source_file"],
                    "page_no": r["page_no"]}).rowcount
            else:
                ins_r += conn.execute(text(_RAKE_INSERT), {
                    "report_date": r["report_date"], "rake_id": r["rake_id"],
                    "track": r["track"],
                    "placed_at": _resolve_placed_at(r["report_date"], r["placed_at"]),
                    "placed_raw": r["placed_at"],
                    "discharge": json.dumps(r["discharge"] or {}),
                    "source_file": r["source_file"],
                    "page_no": r["page_no"]}).rowcount

    print(f"\n  inserted: {ins_p} pendency row(s), {ins_r} rake row(s)")
    with engine.connect() as conn:
        for tbl in ("icd_fpd_pendency", "icd_rake_movement"):
            n = conn.execute(text(f"SELECT count(*) FROM core.{tbl}")).scalar_one()
            span = conn.execute(text(
                f"SELECT min(report_date), max(report_date) FROM core.{tbl}")).one()
            print(f"  core.{tbl:<20} {n:>6} rows   {span[0]} .. {span[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
