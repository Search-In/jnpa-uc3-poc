#!/usr/bin/env python3
"""Idempotent importer for the JNPA Berthing Reports (UC-III module 7).

Walks the five terminal folders under the Berthing Reports data dir, parses each
per-terminal daily PDF into the normalised vessel-call model
(services.berthing.pdf_parsers), and UPSERTS them into the ADDITIVE tables from
migration 0036 via services.berthing.repository.BerthingRepository.persist:

    APM Terminals   -> APMT      BMCT_PSA -> BMCT      NSFT -> NSFT
    NSICT_DP World  -> NSICT     NSIGT_DP World -> NSIGT

Each PDF is ledgered as one jnpa.berthing_import_files row (physical_format='PDF',
source='DIRECTORY'); its bytes are sha256-deduped so re-running is a safe no-op
(SKIPPED_DUPLICATE). Vessel calls upsert on (terminal, voyage_number, vessel_name):
consecutive daily snapshots advance the lifecycle status and fill timestamps, and
lifecycle events accrue idempotently. Purely additive — never touches cargo /
shipping_lines / cfs_ecy / customs / vehicle / driver tables.

NOTE on APMT/BMCT "Expected" sections: those rows are VIA-first with service/line
codes interleaved, so the vessel-name boundary is ambiguous and they are SKIPPED
(only the clean berth-anchored on-berth + sailed rows import). NSFT and DP World
Expected rows ARE serial/vessel-anchored and import cleanly.

Usage:
    python scripts/import_berthing_reports.py --dry-run        # parse only, no DB
    POSTGRES_DSN='postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres' \
        .venv/bin/python scripts/import_berthing_reports.py    # live upsert (+ ensures schema)
Options: --data-dir PATH, --dsn, --dry-run, --no-ensure.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

from services.berthing import pdf_parsers as PP  # noqa: E402

DEFAULT_DATA_DIR = "/Users/pandurangdhage/Downloads/Digital Twin/Data/7-Berthing Reports"
DEFAULT_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres",
)


def collect(data_dir: str) -> List[Dict[str, Any]]:
    """One entry per PDF: {folder, terminal, kind, path, filename, content, records}."""
    out: List[Dict[str, Any]] = []
    for folder, (terminal, kind) in PP.TERMINALS.items():
        d = Path(data_dir) / folder
        if not d.is_dir():
            print(f"  WARN: terminal folder missing: {d}")
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".pdf"):
                continue
            path = d / fn
            content = path.read_bytes()
            try:
                records = PP.parse_pdf_bytes(content, terminal, kind, filename=fn)
            except ValueError as exc:
                print(f"  WARN: could not parse {fn}: {exc}")
                records = []
            out.append({"folder": folder, "terminal": terminal, "kind": kind,
                        "path": str(path), "filename": fn, "content": content,
                        "records": records})
    return out


async def run_import(files: List[Dict[str, Any]], dsn: str, ensure: bool) -> Dict[str, Any]:
    from services.berthing import BerthingRepository

    if ensure:
        from gateway.berthing_ext import ensure_berthing_schema
        await ensure_berthing_schema(dsn)
    repo = BerthingRepository(dsn)
    inserted = updated = skipped_files = 0
    per_terminal: Counter = Counter()
    for f in files:
        sha = hashlib.sha256(f["content"]).hexdigest()
        res = await repo.persist(f["records"], terminal=f["terminal"], filename=f["filename"],
                                 file_hash=sha, physical_format="PDF", file_size=len(f["content"]),
                                 uploaded_by="importer", source="DIRECTORY")
        if res["status"] == "SKIPPED_DUPLICATE":
            skipped_files += 1
        inserted += res.get("inserted", 0)
        updated += res.get("updated", 0)
        per_terminal[f["terminal"]] += res.get("inserted", 0) + res.get("updated", 0)
    return {"inserted": inserted, "updated": updated, "skipped_files": skipped_files,
            "per_terminal": dict(per_terminal)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-ensure", action="store_true",
                    help="skip the boot-ensure DDL (assume migration 0036 already applied)")
    args = ap.parse_args()

    if not Path(args.data_dir).is_dir():
        raise SystemExit(f"FATAL: data dir not found: {args.data_dir}")

    files = collect(args.data_dir)
    # Parse-side report (no DB needed).
    per_term_rows: Counter = Counter()
    per_term_status: Dict[str, Counter] = {}
    total_rows = 0
    for f in files:
        per_term_rows[f["terminal"]] += len(f["records"])
        st = per_term_status.setdefault(f["terminal"], Counter())
        for r in f["records"]:
            st[r["status"]] += 1
        total_rows += len(f["records"])

    live = None
    if not args.dry_run:
        live = asyncio.run(run_import(files, args.dsn, ensure=not args.no_ensure))

    print("\n" + "=" * 70)
    print("BERTHING REPORTS IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  data dir       : {args.data_dir}")
    print(f"  files processed: {len(files)}")
    print(f"  rows detected  : {total_rows}")
    print("-" * 70)
    print("  TERMINAL-WISE:")
    for terminal in ("APMT", "BMCT", "NSFT", "NSICT", "NSIGT"):
        nfiles = sum(1 for f in files if f["terminal"] == terminal)
        rows = per_term_rows.get(terminal, 0)
        st = dict(per_term_status.get(terminal, {}))
        print(f"    {terminal:<6} files={nfiles}  rows={rows:<4} {st}")
    print("-" * 70)
    if args.dry_run:
        print("  rows imported  : n/a (dry-run)")
    else:
        print(f"  new vessel calls (inserted): {live['inserted']}")
        print(f"  updated vessel calls       : {live['updated']}")
        print(f"  duplicate files skipped    : {live['skipped_files']}")
        print(f"  per-terminal upserts       : {live['per_terminal']}")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
