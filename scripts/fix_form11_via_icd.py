#!/usr/bin/env python3
"""Correct `via` and `icd_location` on rows already in core.form11_entry (GAP-RAIL-01).

Why a separate script rather than re-running the importer
---------------------------------------------------------
`Form11IcdService` is idempotent by design in two ways that both prevent a
re-import from fixing anything:

  * identical bytes are ledgered SKIPPED_DUPLICATE on `source_sha256`, so the
    same workbook is never parsed twice; and
  * domain rows insert `ON CONFLICT (terminal, container_no,
    COALESCE(booking_number,'')) DO NOTHING`.

Those properties are correct and worth keeping. So the parser fix only reaches
rows that were loaded BEFORE it, which is what this script is for.

What it changes
---------------
Two fields, on rows that carry the specific wrong values the parser produced:

  * `via IS NULL`      -> the workbook's TERMINAL_VISIT_NUMBER (AGMS0654, SRES0711).
    The DP-World layout names the vessel visit differently from the BMCT sheet
    and only the latter was aliased, so two of three rows had no vessel and the
    rail hop could not join to a call.
  * `icd_location = 'INNSA1'` -> PORT_OF_ORIGIN (INBLR, INTKD).
    INNSA1 is JNPA itself — the port the pre-advice is lodged against. It was
    picked up by a generic `location` alias, which made every rail box read as
    though it originated at the very port it was travelling to.

Safety
------
* UPDATE only, on `core.form11_entry`, matched by (terminal, container_no).
* Each UPDATE carries its own guard (`WHERE via IS NULL`, `WHERE icd_location =
  'INNSA1'`), so a second run reports 0 changed rows. Idempotent by construction.
* New values are re-derived from the corpus workbooks at run time, never
  hardcoded — if the file says something else, the file wins.
* `--dry-run` prints the before/after for every row and touches nothing.
* No DDL, no DELETE, no constraint changes. `jnpa_schema_v3` is shared.

Usage
-----
    python scripts/fix_form11_via_icd.py --data-dir "<corpus>/Data/10-Form 11_ICD Rail" --dry-run
    python scripts/fix_form11_via_icd.py --data-dir "<corpus>/Data/10-Form 11_ICD Rail"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "") or os.environ.get("JNPA_RDS_DSN", "")

#: The value that means "JNPA itself" and is therefore never a valid ICD.
_PORT_NOT_AN_ICD = "INNSA1"


def parse_corpus(data_dir: str) -> List[Dict[str, Any]]:
    """Re-parse the Form 11 workbooks with the CURRENT parser."""
    from services.rail.parsers import form11_icd

    rows: List[Dict[str, Any]] = []
    for p in sorted(Path(data_dir).rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        if p.suffix.lower() not in (".xlsx", ".xls"):
            continue
        res = form11_icd.parse(p.read_bytes(), p.name)
        if res.feed != "FORM11" or res.rejected:
            continue
        rows.extend(res.rows)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dsn:
        print("No DSN: set POSTGRES_DSN or pass --dsn", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text

    parsed = parse_corpus(args.data_dir)
    if not parsed:
        print("No Form 11 rows parsed — nothing to do.", file=sys.stderr)
        return 1

    print("FORM 11 via / icd_location CORRECTION (GAP-RAIL-01)"
          + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print(f"  parsed {len(parsed)} Form 11 row(s) from {args.data_dir}\n")

    engine = create_engine(args.dsn.replace("postgresql://", "postgresql+psycopg://"))
    changed_via = changed_icd = 0

    with engine.begin() as conn:
        for r in parsed:
            key = {"terminal": r["terminal"], "container_no": r["container_no"]}
            before = conn.execute(text(
                "SELECT via, icd_location FROM core.form11_entry "
                "WHERE terminal = :terminal AND container_no = :container_no"
            ), key).fetchone()
            if before is None:
                print(f"  - {r['container_no']}: not in the database; skipped "
                      "(this script corrects existing rows, it does not insert)")
                continue

            print(f"  {r['terminal']:<6} {r['container_no']}")
            print(f"      via          {before[0]!r} -> {r['via']!r}")
            print(f"      icd_location {before[1]!r} -> {r['icd_location']!r}")

            if args.dry_run:
                continue

            # Guarded so a re-run is a no-op, and so a value someone else has
            # since corrected by hand is never overwritten.
            if r["via"]:
                changed_via += conn.execute(text(
                    "UPDATE core.form11_entry SET via = :via "
                    "WHERE terminal = :terminal AND container_no = :container_no "
                    "  AND via IS NULL"
                ), {**key, "via": r["via"]}).rowcount
            if r["icd_location"]:
                changed_icd += conn.execute(text(
                    "UPDATE core.form11_entry SET icd_location = :icd "
                    "WHERE terminal = :terminal AND container_no = :container_no "
                    "  AND (icd_location IS NULL OR icd_location = :not_an_icd)"
                ), {**key, "icd": r["icd_location"],
                    "not_an_icd": _PORT_NOT_AN_ICD}).rowcount

    if args.dry_run:
        print("\nDRY-RUN complete — nothing written.")
        return 0

    print(f"\n  via corrected          : {changed_via} row(s)")
    print(f"  icd_location corrected : {changed_icd} row(s)")

    with engine.connect() as conn:
        print("\nRESULT — core.form11_entry:")
        for row in conn.execute(text(
            "SELECT terminal, container_no, via, icd_location "
            "FROM core.form11_entry ORDER BY terminal"
        )):
            print("   ", tuple(row))
        unnamed = conn.execute(text(
            "SELECT count(*) FROM core.form11_entry WHERE via IS NULL"
        )).scalar_one()
        print(f"\n  rows still without a vessel visit: {unnamed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
