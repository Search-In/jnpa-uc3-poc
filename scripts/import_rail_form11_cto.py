#!/usr/bin/env python3
"""Idempotent directory importer for the Form 11 / CTO rail group (corpus group 10).

Counterpart to `import_rail_fois.py`. `Form11IcdService.import_file()` already
handled both shapes — Form 11 workbooks (.xlsx) and CTO rake manifests (.txt) —
but only the HTTP upload route called it, so the three Form 11 workbooks were
never loaded: `core.form11_entry` sat at 0 rows while the files sat on disk.

Purely additive; each file is sha256-ledgered in `core.rail_import_file`, so a
second run reports SKIPPED_DUPLICATE for every file.

The 14 `ICD_REPORTS/*.pdf` are submitted too. The service classifies them
REJECTED / UNSUPPORTED_FORMAT and ledgers each with a reason, which is the honest
record: those reports are a PDF layout nothing in the codebase parses yet — a
missing parser, not a wiring gap. Leaving them out of the run entirely would have
made the import look complete when a fourteenth of the group is unread.

Usage:
    python scripts/import_rail_form11_cto.py --data-dir "<corpus>/Data/10-Form 11_ICD Rail" --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")

#: Extensions the Form 11 / CTO parser understands. Anything else is still SENT
#: to the service, which ledgers it as REJECTED/UNSUPPORTED_FORMAT with a reason —
#: a file that is on disk and unreadable should appear in the provenance ledger
#: saying so, not be silently absent from the run.
_SUPPORTED = (".xlsx", ".xls", ".txt")


def collect(data_dir: str) -> Dict[str, List[Path]]:
    root = Path(data_dir)
    supported, unparsed = [], []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.name.startswith("."):
            continue
        # FOIS intimations in this folder belong to the FOIS importer.
        if "train intimation" in p.name.lower():
            continue
        supported.append(p)
        if p.suffix.lower() not in _SUPPORTED:
            unparsed.append(p)
    return {"supported": supported, "unparsed": unparsed}


async def run_import(paths: List[Path], dsn: str) -> Dict[str, Any]:
    from services.rail import Form11IcdService
    svc = Form11IcdService(dsn=dsn)
    tally: Counter = Counter()
    feeds: Counter = Counter()
    imported = skipped = invalid = 0
    for p in paths:
        res = await svc.import_file(p.read_bytes(), p.name, uploaded_by="importer")
        tally[res["status"]] += 1
        feeds[res.get("feed") or "?"] += res.get("imported", 0)
        imported += res.get("imported", 0)
        skipped += res.get("skipped", 0)
        invalid += res.get("invalid", 0)
        if res["status"] == "REJECTED":
            print(f"  WARN: rejected {p.name}: {res.get('reason')}")
    return {"status_tally": dict(tally), "rows_per_feed": dict(feeds),
            "imported": imported, "skipped": skipped, "invalid": invalid}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not Path(args.data_dir).is_dir():
        raise SystemExit(f"FATAL: data dir not found: {args.data_dir}")

    found = collect(args.data_dir)
    live = None
    if not args.dry_run:
        live = asyncio.run(run_import(found["supported"], args.dsn))

    root = Path(args.data_dir)
    print("\n" + "=" * 70)
    print("RAIL FORM 11 / CTO IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  data dir        : {args.data_dir}")
    print(f"  files submitted : {len(found['supported'])}")
    for p in found["supported"]:
        if p.suffix.lower() in _SUPPORTED:
            print(f"      {p.relative_to(root)}")
    if found["unparsed"]:
        print(f"  no parser yet   : {len(found['unparsed'])}  (ledgered as REJECTED with a reason)")
        for p in found["unparsed"][:4]:
            print(f"      {p.relative_to(root)}")
        if len(found["unparsed"]) > 4:
            print(f"      … and {len(found['unparsed']) - 4} more")
    if live:
        print("-" * 70)
        print(f"  status tally    : {live['status_tally']}")
        print(f"  rows per feed   : {live['rows_per_feed']}")
        print(f"  rows imported   : {live['imported']}")
        print(f"  rows duplicate  : {live['skipped']}")
        print(f"  rows invalid    : {live['invalid']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
