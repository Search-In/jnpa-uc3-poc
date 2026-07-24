#!/usr/bin/env python3
"""Idempotent importer for the official JNPA Shipping Lines files (module 4).

Loads EVERY customer file under $SHIPPING_LINES_DATA_DIR into the shipping-line
schema (migration 0032). The customer folder layout:

    4-Shipping Lines/
      IAL FORMAT/  IAL <terminal>.{csv,xlsx}   Import Advance List -> sl_advance_containers
      EAL_FORMAT/  EAL_<terminal>.{csv,xls}    Export Advance List -> sl_advance_containers
      EDO/         EDO.xlsx (CODECO XML)        Electronic Delivery Order -> sl_delivery_orders

The data is the ONLY source of truth — NOTHING is synthesised. Re-running is a
no-op: import is idempotent on file content (sl_import_files.source_sha256) and on
every child's natural key (ON CONFLICT DO NOTHING). Purely additive — it never
touches cargo / customs / gate / auth tables; container_no soft-links to core.cargo
BY VALUE.

=== DATABASE TARGET (RDS-only) ===
This importer writes to the EXISTING JNPA database via the project's standard
POSTGRES_DSN — the SAME connection used by Cargo / Customs / CFS-ECY / Performance.
It introduces NO new database configuration. If POSTGRES_DSN is not exported it is
read from the project's active .env.local (the file already cut over to AWS RDS).
Before importing it RUNS A PREFLIGHT: if the resolved DSN does not point at an RDS
endpoint (e.g. localhost / 127.0.0.1 / a Docker `postgres` service / port 5433) it
STOPS and asks for confirmation (--allow-non-rds) instead of writing. Credentials
are never printed.

Usage:
    # dry-run: parse + validate every file, no DB writes, no DSN needed
    .venv/bin/python scripts/import_shipping_lines.py --dry-run

    # live import into RDS (POSTGRES_DSN from env or .env.local; ensures schema first)
    .venv/bin/python scripts/import_shipping_lines.py

Options: --data-dir PATH (else $SHIPPING_LINES_DATA_DIR), --dsn, --dry-run,
         --no-ensure, --allow-non-rds, --no-validate.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

_LOCAL_HOST_MARKERS = ("localhost", "127.0.0.1", "::1", "postgres", "db",
                       "host.docker.internal", "0.0.0.0")


def _resolve_dsn(cli_dsn: Optional[str]) -> Optional[str]:
    """Reuse the project's existing configuration — NO new DB config.

    Priority: explicit --dsn > $POSTGRES_DSN > the active .env.local (already RDS).
    Returns None if none is available (the caller then aborts)."""
    if cli_dsn:
        return cli_dsn
    env = os.environ.get("POSTGRES_DSN")
    if env:
        return env
    # Fall back to the project's active env file — same file every service uses.
    env_file = _ROOT / ".env.local"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values
            return dotenv_values(str(env_file)).get("POSTGRES_DSN")
        except Exception:  # noqa: BLE001 — dotenv is a dev dep; degrade gracefully
            for line in env_file.read_text().splitlines():
                if line.strip().startswith("POSTGRES_DSN="):
                    return line.split("=", 1)[1].strip()
    return None


def _dsn_target(dsn: str) -> tuple[str, Optional[int], str]:
    """Return (host, port, db) — never the password — for the preflight report."""
    try:
        from sqlalchemy.engine import make_url
        url = make_url(dsn)
        return (url.host or "?"), url.port, (url.database or "?")
    except Exception:  # noqa: BLE001
        return "?", None, "?"


def _preflight_rds(dsn: str, *, allow_non_rds: bool) -> bool:
    """Verify the DSN targets RDS BEFORE any write. Returns True to proceed.

    Prints host/port/db (no credentials). Stops on a localhost / non-RDS target
    unless --allow-non-rds is given."""
    host, port, db = _dsn_target(dsn)
    print(f"DB target: host={host} port={port} db={db}")
    is_rds = host.endswith(".rds.amazonaws.com")
    looks_local = any(m in host.lower() for m in _LOCAL_HOST_MARKERS) or port == 5433
    if is_rds:
        print("Preflight: RDS endpoint confirmed.")
        return True
    if allow_non_rds:
        print("Preflight: NON-RDS target, but --allow-non-rds was passed — proceeding.")
        return True
    reason = "localhost / local Postgres" if looks_local else "not an *.rds.amazonaws.com endpoint"
    print(f"\nSTOP: the configured database is {reason}.", file=sys.stderr)
    print("This importer targets the existing AWS RDS database only. Point POSTGRES_DSN "
          "at RDS (or the project .env.local), or pass --allow-non-rds to override.",
          file=sys.stderr)
    return False


async def _validate(dsn: str) -> None:
    """Post-import validation against the SAME database (RDS): imported files, rows,
    duplicates, failures, shipping-line count, container count, terminal-wise counts."""
    from jnpa_shared.db import fetch_all, fetch_one
    print("\n=== VALIDATION (live DB) ===")
    files = await fetch_one(
        "SELECT count(*) AS files, "
        "count(*) FILTER (WHERE import_status='SUCCESS') AS ok, "
        "count(*) FILTER (WHERE import_status='SKIPPED_DUPLICATE') AS duplicate, "
        "count(*) FILTER (WHERE import_status='FAILED') AS failed, "
        "sum(record_count) AS records, sum(imported_count) AS imported "
        "FROM core.sl_import_file", dsn=dsn)
    print(f"  files: {dict(files)}")
    totals = await fetch_one(
        "SELECT (SELECT count(*) FROM core.advance_list_container) AS advance_containers, "
        "(SELECT count(DISTINCT container_no) FROM core.advance_list_container) AS distinct_containers, "
        "(SELECT count(*) FROM core.delivery_order_line) AS delivery_orders, "
        "(SELECT count(*) FROM core.ref_shipping_line) AS shipping_lines, "
        "(SELECT count(*) FROM core.advance_list_container WHERE bill_of_lading IS NOT NULL) AS with_bl, "
        "(SELECT count(*) FROM core.sl_import_error) AS row_errors", dsn=dsn)
    print(f"  rows:  {dict(totals)}")
    terms = await fetch_all(
        "SELECT terminal, list_type, count(*) AS n FROM core.advance_list_container "
        "GROUP BY terminal, list_type ORDER BY terminal, list_type", dsn=dsn)
    print("  terminal-wise:")
    for r in terms:
        print(f"    {r['terminal']:6} {r['list_type']:4} {r['n']:6}")
    lines = await fetch_all(
        "SELECT shipping_line_code AS line, count(*) AS n FROM core.advance_list_container "
        "WHERE shipping_line_code IS NOT NULL GROUP BY shipping_line_code "
        "ORDER BY n DESC LIMIT 10", dsn=dsn)
    print("  top shipping lines: " + ", ".join(f"{r['line']}={r['n']}" for r in lines))
    dupes = await fetch_one(
        "SELECT count(*) AS dupe_container_no FROM (SELECT container_no FROM core.advance_list_container "
        "GROUP BY container_no HAVING count(*) > 1) t", dsn=dsn)
    print(f"  containers appearing in >1 list row: {dupes['dupe_container_no']} "
          "(expected — a box recurs across terminals / IAL+EAL)")


async def _run(data_dir: str, dsn: Optional[str], *, dry_run: bool, ensure: bool,
               allow_non_rds: bool, validate: bool) -> int:
    from services.shipping_lines.service import ShippingLinesService, detect_format
    from services.shipping_lines.parsers import ShippingLineParseError

    if not os.path.isdir(data_dir):
        print(f"ERROR: shipping-lines data dir not found: {data_dir}", file=sys.stderr)
        return 2

    if dry_run:
        files = ShippingLinesService._discover(data_dir)
        total = 0
        by_list: dict[str, int] = {}
        for path in files:
            name = os.path.relpath(path, data_dir)
            try:
                list_type, terminal, _fmt = detect_format(path)
                from services.shipping_lines.service import _parse
                pl = _parse(path, list_type, terminal)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL   {name}: {exc}")
                continue
            total += pl.record_count
            by_list[list_type] = by_list.get(list_type, 0) + pl.record_count
            print(f"  OK     {list_type:3} {terminal:6} records={pl.record_count:5}  {name}")
        print(f"\nDRY-RUN totals: {total} records  {by_list}")
        return 0

    # ---- live import: resolve + preflight the DB target (RDS-only) ----
    if dsn is None:
        print("ERROR: no POSTGRES_DSN configured (env or .env.local). Refusing to guess.",
              file=sys.stderr)
        return 2
    if not _preflight_rds(dsn, allow_non_rds=allow_non_rds):
        return 3

    if ensure:
        from gateway.shipping_lines_ext import ensure_shipping_lines_schema
        await ensure_shipping_lines_schema(dsn)

    svc = ShippingLinesService(dsn)
    summary = await svc.import_directory(data_dir)
    for r in summary["results"]:
        print(f"  {r['import_status']:17} {str(r.get('list_type')):3} {str(r.get('terminal')):6} "
              f"rec={r['record_count']:5} imp={r['imported_count']:5}  {r['source_file']}")
    t = summary["totals"]
    print(f"\nIMPORT complete: {t['files']} files "
          f"({t['succeeded']} ok, {t['duplicate']} duplicate, {t['failed']} failed) — "
          f"{t['records']} records, {t['imported']} imported")

    if validate:
        await _validate(dsn)

    from jnpa_shared.db import dispose_all
    await dispose_all()
    return 1 if t["failed"] else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Import official JNPA shipping-line files (module 4) into RDS.")
    ap.add_argument("--data-dir", default=None,
                    help="customer data dir (else $SHIPPING_LINES_DATA_DIR / default)")
    ap.add_argument("--dsn", default=None, help="override POSTGRES_DSN (default: env / .env.local)")
    ap.add_argument("--dry-run", action="store_true", help="parse + validate only, no DB, no DSN")
    ap.add_argument("--no-ensure", action="store_true", help="skip ensure_shipping_lines_schema")
    ap.add_argument("--allow-non-rds", action="store_true",
                    help="override the RDS preflight (DANGEROUS — writes to a non-RDS DB)")
    ap.add_argument("--no-validate", action="store_true", help="skip post-import validation")
    args = ap.parse_args()

    from services.shipping_lines.service import DEFAULT_DATA_DIR
    data_dir = args.data_dir or DEFAULT_DATA_DIR
    dsn = None if args.dry_run else _resolve_dsn(args.dsn)
    return asyncio.run(_run(data_dir, dsn, dry_run=args.dry_run, ensure=not args.no_ensure,
                            allow_non_rds=args.allow_non_rds, validate=not args.no_validate))


if __name__ == "__main__":
    raise SystemExit(main())
