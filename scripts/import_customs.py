#!/usr/bin/env python3
"""Idempotent importer for the official JNPA Customs files (module 5).

Loads EVERY customer file under $CUSTOMS_DATA_DIR into the customs schema
(migration 0031). The customer folder layout:

    5- Customs/
      IGM/  CHPOI03_*.xml     Import General Manifest      -> customs_igm_*
      OOC/  CHPOI10_*.xml     Out-Of-Charge / Bill-of-Entry-> customs_ooc*
      SMTP/ CHPOI13_*.xml     Transhipment Permit          -> customs_smtp*
      RMS/  *.txt             Scanning selection list      -> customs_rms_*
      LEO/  leodetails.xlsx   Let Export Order             -> customs_leo
      Shipping Bill/*.xlsx    Shipping Bill                -> customs_shipping_bill

The data is the ONLY source of truth — NOTHING is synthesised. Re-running is a
no-op: import is idempotent on file content (customs_messages.source_sha256) and on
every child's natural key. Purely additive — it never touches cargo / gate / auth
tables; container_no soft-links to jnpa.cargo BY VALUE.

Usage:
    # dry-run: parse + validate every file, no DB writes
    .venv/bin/python scripts/import_customs.py --dry-run

    # live import (ensures schema first)
    POSTGRES_DSN='postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres' \
        .venv/bin/python scripts/import_customs.py

Options: --data-dir PATH (else $CUSTOMS_DATA_DIR), --dsn, --dry-run, --no-ensure.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DATA_DIR = os.environ.get(
    "CUSTOMS_DATA_DIR",
    os.path.expanduser("~/Downloads/Digital Twin/data/5- Customs"))
DEFAULT_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres")


async def _run(data_dir: str, dsn: str, *, dry_run: bool, ensure: bool,
               reconcile: bool = False) -> int:
    from services.customs.service import CustomsService, detect_parser
    from services.customs.service import UnknownCustomsFormat
    from services.customs.parsers import CustomsParseError

    if not os.path.isdir(data_dir):
        print(f"ERROR: customs data dir not found: {data_dir}", file=sys.stderr)
        return 2

    if dry_run:
        # Parse + validate every file; report counts; touch no DB.
        files = CustomsService._discover(data_dir)
        total = 0
        by_module: dict[str, int] = {}
        for path in files:
            name = os.path.basename(path)
            try:
                parser, module = detect_parser(path)
                pm = parser(path)
            except (CustomsParseError, UnknownCustomsFormat) as exc:
                print(f"  FAIL   {name}: {exc}")
                continue
            total += pm.record_count
            by_module[module] = by_module.get(module, 0) + pm.record_count
            print(f"  OK     {module:13} records={pm.record_count:5}  {name}")
        print(f"\nDRY-RUN totals: {total} records  {by_module}")
        return 0

    if ensure:
        from gateway.customs_ext import ensure_customs_schema
        await ensure_customs_schema(dsn)

    svc = CustomsService(dsn)
    summary = await svc.import_directory(data_dir)
    for r in summary["results"]:
        print(f"  {r['import_status']:17} {str(r.get('module')):13} "
              f"rec={r['record_count']:5} imp={r['imported_count']:5}  {r['source_file']}")
    t = summary["totals"]
    print(f"\nIMPORT complete: {t['files']} files "
          f"({t['succeeded']} ok, {t['duplicate']} duplicate, {t['failed']} failed) — "
          f"{t['records']} records, {t['imported']} imported")

    if reconcile:
        rc = await svc.reconcile_cargo()
        print(f"RECONCILE: {rc['cleared']} container(s) -> CLEARED, "
              f"{rc['under_inspection']} -> UNDER_INSPECTION (bound to jnpa.cargo)")

    from jnpa_shared.db import dispose_all
    await dispose_all()
    return 1 if t["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import official JNPA customs files (module 5).")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true", help="parse + validate only, no DB")
    ap.add_argument("--no-ensure", action="store_true", help="skip ensure_customs_schema")
    ap.add_argument("--reconcile", action="store_true",
                    help="after import, bind customs docs to jnpa.cargo.customs_status")
    args = ap.parse_args()
    return asyncio.run(_run(args.data_dir, args.dsn, dry_run=args.dry_run,
                            ensure=not args.no_ensure, reconcile=args.reconcile))


if __name__ == "__main__":
    raise SystemExit(main())
