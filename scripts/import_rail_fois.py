#!/usr/bin/env python3
"""Idempotent directory importer for the JNPA FOIS train-intimation CSVs.

WHY THIS EXISTS. `RailFoisService.import_file()` — with its sha256 ledger dedup —
already existed, but only the HTTP upload route ever called it. The corpus ships
35 FOIS files: one at `9-NLDS_FOIS/` root and **34 more inside `9-NLDS_FOIS/FOIS/`**
covering 20 Jun – 25 Jul 2026, and nothing walked that subfolder. The 34 were
registered by the file-inventory ETL and then dropped, so `core.rake` held a
single file's worth (59 rows, 8–11 May) while a month of rail arrivals sat unread
on disk.

The headers are byte-identical to the parsed file apart from a line ending, so no
new parser was needed — only something to feed the existing one.

Purely additive. Each file is sha256-ledgered in `core.rail_import_file`, so a
second run is a no-op (every file reports SKIPPED_DUPLICATE).

Usage:
    python scripts/import_rail_fois.py --data-dir "<corpus>/Data/9-NLDS_FOIS" --dry-run
    POSTGRES_DSN=... python scripts/import_rail_fois.py --data-dir "<...>"
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

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")


def collect(data_dir: str) -> List[Path]:
    """Every FOIS CSV under the group folder, recursively.

    Recursive on purpose: the corpus splits them between the group root and a
    `FOIS/` subfolder, and a non-recursive walk silently reads 1 of 35.
    """
    root = Path(data_dir)
    files = [p for p in sorted(root.rglob("*.csv"))
             if "train intimation" in p.name.lower()]
    return files


async def run_import(paths: List[Path], dsn: str) -> Dict[str, Any]:
    from services.rail.fois_service import RailFoisService
    svc = RailFoisService(dsn=dsn)
    tally: Counter = Counter()
    imported = skipped = invalid = 0
    for p in paths:
        content = p.read_bytes()
        res = await svc.import_file(content, p.name, uploaded_by="importer")
        tally[res["status"]] += 1
        imported += res.get("imported", 0)
        skipped += res.get("skipped", 0)
        invalid += res.get("invalid", 0)
        if res["status"] == "REJECTED":
            print(f"  WARN: rejected {p.name}: {res.get('reason')}")
    return {"status_tally": dict(tally), "imported": imported,
            "skipped": skipped, "invalid": invalid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True,
                    help="the 9-NLDS_FOIS group folder (walked recursively)")
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not Path(args.data_dir).is_dir():
        raise SystemExit(f"FATAL: data dir not found: {args.data_dir}")

    paths = collect(args.data_dir)
    rows = 0
    for p in paths:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            rows += max(0, sum(1 for _ in fh) - 1)

    live = None
    if not args.dry_run:
        live = asyncio.run(run_import(paths, args.dsn))

    print("\n" + "=" * 70)
    print("RAIL FOIS IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  data dir       : {args.data_dir}")
    print(f"  files found    : {len(paths)}")
    print(f"  data rows      : {rows}")
    for p in paths[:3]:
        print(f"    e.g. {p.relative_to(Path(args.data_dir))}")
    if live:
        print("-" * 70)
        print(f"  status tally   : {live['status_tally']}")
        print(f"  rows imported  : {live['imported']}")
        print(f"  rows duplicate : {live['skipped']}")
        print(f"  rows invalid   : {live['invalid']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
