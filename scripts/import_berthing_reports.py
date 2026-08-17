#!/usr/bin/env python3
"""Idempotent importer for the JNPA Berthing Reports (UC-III module 7).

Walks the five terminal folders under the Berthing Reports data dir, parses each
per-terminal daily PDF into the normalised vessel-call model
(services.berthing.pdf_parsers), and UPSERTS them into the ADDITIVE tables from
migration 0036 via services.berthing.repository.BerthingRepository.persist:

    APM Terminals   -> APMT      BMCT_PSA -> BMCT      NSFT -> NSFT
    NSICT_DP World  -> NSICT     NSIGT_DP World -> NSIGT

Each PDF is ledgered as one core.berthing_import_file row (physical_format='PDF',
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
    POSTGRES_DSN='postgresql+asyncpg://postgres:$RDS_PW@__RDS_HOST__:5432/jnpa_schema_v3?ssl=require' \
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

DEFAULT_DATA_DIR = (
    Path(__file__).resolve().parents[2]
    / "jnpa_poc_1"
    / "data"
    / "7 Berthing Report"
)
# Fall back to the historic Downloads path when the sibling PoC corpus is absent.
if not DEFAULT_DATA_DIR.is_dir():
    DEFAULT_DATA_DIR = Path(
        "/Users/pandurangdhage/Downloads/Digital Twin/Data/7-Berthing Reports"
    )
DEFAULT_DATA_DIR = str(DEFAULT_DATA_DIR)
# Application database = AWS RDS (jnpa_schema_v3). No local-postgres fallback:
# set POSTGRES_DSN (or pass --dsn) or the script refuses to run.
DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")


def collect(data_dir: str) -> List[Dict[str, Any]]:
    """One entry per PDF: {folder, terminal, kind, path, filename, content, records}.

    Supports both corpus layouts, and — since 17-Aug-2026 — BOTH AT ONCE:
      * Classic — ``APM Terminals/``, ``BMCT_PSA/``, … (TERMINALS map)
      * Week    — ``2026-07-20_Mon/APMT_2026-07-20.pdf`` (filename prefix)

    These used to be either/or: the classic branch returned early, so whenever a
    corpus contained both — which the shipped one does — the dated week tree was
    never walked. The effect was silent: the import reported success having read
    25 of 59 files, and the 20–26 July week (the only week with a complete set of
    daily reports across all five terminals) was simply absent from the database.
    Both are now collected, de-duplicated by resolved path.
    """
    root = Path(data_dir)
    out: List[Dict[str, Any]] = []
    seen: set = set()
    classic = any((root / folder).is_dir() for folder in PP.TERMINALS)

    if classic:
        for folder, (terminal, kind) in PP.TERMINALS.items():
            d = root / folder
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
                seen.add(path.resolve())
                out.append({"folder": folder, "terminal": terminal, "kind": kind,
                            "path": str(path), "filename": fn, "content": content,
                            "records": records})
        # NO early return — fall through so a corpus carrying BOTH layouts has
        # its dated week tree collected too.

    # Week layout (date folders with APMT_/NSICT_/… filenames)
    for path in sorted(root.rglob("*.pdf")):
        if path.resolve() in seen:
            continue
        fn = path.name
        content = path.read_bytes()
        det = PP.terminal_from_filename(fn)
        try:
            if det is not None:
                terminal, kind = det
                records = PP.parse_pdf_bytes(content, terminal, kind, filename=fn)
            else:
                records, terminal = PP.parse_pdf_bytes_auto(content, filename=fn)
                kind = PP._KIND_FOR_TERMINAL[terminal]
        except ValueError as exc:
            print(f"  WARN: skip {path.relative_to(root)}: {exc}")
            continue
        seen.add(path.resolve())
        out.append({"folder": path.parent.name, "terminal": terminal, "kind": kind,
                    "path": str(path), "filename": fn, "content": content,
                    "records": records})
    if not out:
        print(f"  WARN: no berthing PDFs under {root}")
    return out


async def run_import(files: List[Dict[str, Any]], dsn: str, ensure: bool) -> Dict[str, Any]:
    from services.berthing import BerthingRepository
    from services.berthing.document_repository import BerthingDocumentRepository
    from services.berthing.full_extractor import extract_tables
    from services.berthing.pdf_store import store_pdf

    if ensure:
        from gateway.berthing_ext import ensure_berthing_schema
        await ensure_berthing_schema(dsn)
    repo = BerthingRepository(dsn)
    docs = BerthingDocumentRepository(dsn)
    inserted = updated = skipped_files = 0
    docs_imported = docs_skipped = 0
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

        # Verbatim tables + original PDF bytes for evidence / source viewer
        try:
            store_pdf(sha, f["content"])
            tables = extract_tables(f["content"], f["filename"])
            doc_res = await docs.persist(tables, pdf_hash=sha, uploaded_by="importer")
            if doc_res.get("status") == "SKIPPED_DUPLICATE":
                docs_skipped += 1
            else:
                docs_imported += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  WARN: full-extract failed for {f['filename']}: {exc}")

    return {"inserted": inserted, "updated": updated, "skipped_files": skipped_files,
            "docs_imported": docs_imported, "docs_skipped": docs_skipped,
            "per_terminal": dict(per_terminal)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        required=not DEFAULT_DSN,
        help="SQLAlchemy asyncpg DSN for the RDS database "
             "(defaults to $POSTGRES_DSN; no local fallback)",
    )
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
        print(f"  verbatim docs imported     : {live.get('docs_imported', 0)}")
        print(f"  verbatim docs skipped      : {live.get('docs_skipped', 0)}")
        print(f"  per-terminal upserts       : {live['per_terminal']}")
    print("=" * 70 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
