#!/usr/bin/env python3
"""Idempotent directory importer for the bathymetry survey PDFs (corpus group 2).

`scripts/ingest_uc1_corpus.py` ingests the sea-channel SHAPEFILE from this group
and nothing else — it never enumerates the survey PDFs. The parser
(`services/marine/parsers/bathymetry_pdf.py`) and its registry entry both existed
already, so 24 of 36 surveys sat unread purely for want of something to walk the
folder: `core.bathymetry_survey` held 12 rows while a full post-dredge series
(A-B, B-C, C-D, D-E, E-F, BMCT and the JNP anchorage) waited on disk.

Those surveys are the controlling-depth evidence behind the UKC console, so the
gap mattered: the twin was reasoning about under-keel clearance from a third of
the available depth data.

Purely additive; `MarineUploadService.import_file()` sha256-dedups each file, so a
second run reports duplicates and writes nothing.

Usage:
    python scripts/import_marine_bathymetry.py --data-dir "<corpus>/Data/2-JNPA_Sea_Channels_Bathymetry" --dry-run
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
UPLOADER = "importer"


def collect(data_dir: str) -> List[Path]:
    """Every PDF under the group, recursively — the corpus splits them between
    `Bathymetry Data/` and `Bathymetry Data/PDF/`."""
    return [p for p in sorted(Path(data_dir).rglob("*.pdf")) if p.is_file()]


async def run_import(paths: List[Path], dsn: str) -> Dict[str, Any]:
    from services.marine import MarineUploadService
    svc = MarineUploadService(dsn=dsn)
    tally: Counter = Counter()
    imported = 0
    rejected: List[str] = []
    for p in paths:
        res = await svc.import_file(p.read_bytes(), p.name, UPLOADER)
        status = str(res.get("status"))
        tally[status] += 1
        imported += res.get("imported", 0) or 0
        if status not in ("SUCCESS", "PARTIAL", "SKIPPED_DUPLICATE"):
            rejected.append(f"{p.name} -> {status}")
    return {"status_tally": dict(tally), "imported": imported, "rejected": rejected}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not Path(args.data_dir).is_dir():
        raise SystemExit(f"FATAL: data dir not found: {args.data_dir}")

    paths = collect(args.data_dir)
    live = None
    if not args.dry_run:
        live = asyncio.run(run_import(paths, args.dsn))

    print("\n" + "=" * 70)
    print("MARINE BATHYMETRY IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  data dir       : {args.data_dir}")
    print(f"  survey PDFs    : {len(paths)}")
    if live:
        print("-" * 70)
        print(f"  status tally   : {live['status_tally']}")
        print(f"  rows imported  : {live['imported']}")
        if live["rejected"]:
            print(f"  not accepted   : {len(live['rejected'])}")
            for r in live["rejected"]:
                print(f"      {r}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
