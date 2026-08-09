#!/usr/bin/env python3
"""UC1-002 — load the complete UC-I marine corpus into the existing database.

A DRIVER, not an ingestion engine. Every byte of parsing, persistence, provenance and
de-duplication already exists and is reused unchanged:

    PCS / pilot card / port craft / sea channel
        -> services.marine.upload_service.MarineUploadService.import_file()
           -> services.marine.parsers.parse_marine()      (envelope + registry routing)
           -> services.marine.repository.VesselCallRepository.persist()
              -> core.marine_import_files   (file ledger, sha256, per-origin UNIQUE)
              -> core.marine_import_errors  (quarantine)
              -> core.vessel / vessel_call / vessel_call_event / pilot / pilotage /
                 port_craft / sea_channel

    Berthing report PDFs
        -> scripts.import_berthing_reports.collect() + run_import()
           -> services.berthing.*  (its own sha256 file ledger + upsert)

This module therefore contains NO parser, NO SQL and NO de-duplication logic of its own.
What it adds is the thing that was missing: a deterministic walk of the corpus, in
dependency order, with one aggregated report.

    # dry run - parses everything, writes nothing, needs no database
    python scripts/ingest_uc1_corpus.py --corpus "../client-data" --dry-run

    # load
    python scripts/ingest_uc1_corpus.py --corpus "../client-data" \
        --dsn postgresql+asyncpg://postgres:...@127.0.0.1:5432/jnpa_v3_local

    # run it again: every file is SKIPPED_DUPLICATE and the counts do not move
    python scripts/ingest_uc1_corpus.py --corpus "../client-data" --dsn ... --verify-only

ORDER MATTERS, and only in one place: VESPRO (vessel master) is loaded before the call
messages so a call can bind to a known IMO. Everything else is order-independent because
every write is an upsert.

IDEMPOTENCE comes entirely from the existing sha256 file ledgers, so it depends on this
script producing byte-identical input on every run. The only file it SYNTHESISES is the
sea-channel ZIP (the corpus ships a loose .shp/.dbf/.prj directory and the parser wants a
bundle), so that ZIP is built deterministically - fixed timestamps, fixed order, no
compression metadata that varies - or run 2 would compute a different hash and re-import.

EXPECTED COUNTS ARE NOT HARDCODED. --expect takes them from the caller (or a file), and
the report prints measured-vs-expected side by side. With no --expect the script simply
reports what it measured.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import zipfile
from collections import Counter
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.marine.upload_service import MarineUploadService  # noqa: E402

#: Ledger attribution for everything this loader imports. It is NOT 'jnpa-api', so
#: services.marine.repository._data_origin tags every row data_origin='MANUAL' and the
#: corpus can never be confused with, or de-duplicated against, the live API feed.
UPLOADED_BY = "uc1-corpus-loader"

DEFAULT_CORPUS = str((_ROOT.parent / "client-data").resolve())


# --------------------------------------------------------------------------- discovery
class Source(tuple):
    """(family, path, filename, loader) - a tuple so the plan is trivially sortable."""


#: Corpus families in load order. Each entry is (family, relative dir, glob, recurse).
#:
#: TWO ordering rules, both load-bearing, both learned from a failed run:
#:
#:   1. VESPRO first. The vessel master must exist before a call binds to an IMO
#:      (core.vessel_call.imo_no -> core.vessel, migration 0044).
#:
#:   2. CALL-PRODUCING families before EVENT-ONLY families. VESARR/VESDEP carry only
#:      milestones (ANCHORED / DEPARTED); they create no call. The repository resolves an
#:      event to its call and records `unresolved_call` rather than inventing a stub call
#:      (persist() step 4, decision 2), so an event whose call has not been created yet is
#:      a row error — and with every row erroring, the file is FAILED. The VCNs those two
#:      logs reference come from the JOURNALS, so the journals must be loaded first.
#:      Ordering them the other way cost both logs entirely: 8 + 12 events, 0 rows.
#:
#: Everything else is order-independent because every write is an upsert.
_MARINE_FAMILIES: Tuple[Tuple[str, str, Tuple[str, ...], bool], ...] = (
    # --- vessel master ---
    ("VESPRO",      "1-NLP Marine/VESPRO",                  (".xml",),               False),
    # --- call-producing families ---
    ("CALINF",      "1-NLP Marine/CALINF",                  (".xml",),               False),
    ("BERMAN",      "1-NLP Marine/BERMAN",                  (".xml",),               False),
    ("JOURNAL_IN",  "1-NLP Marine/Inbound_CALINF_BERMAN",   (".csv",),               True),
    ("JOURNAL_OUT", "1-NLP Marine/Outbound_CALINV_BERALT",  (".csv",),               True),
    # --- event-only families: MUST follow the call producers above ---
    ("VESARR",      "1-NLP Marine/VESARR",                  (".log",),               False),
    ("VESDEP",      "1-NLP Marine/VESDEP",                  (".log",),               False),
    # --- independent registers ---
    ("PILOT_CARD",  "3- Port Craft & Pilot",                (".xlsx",),              False),
    ("PORT_CRAFT",  "3- Port Craft & Pilot",                (".pdf",),               False),
)

#: Families that only emit vessel_call_event records; they depend on a call existing.
_EVENT_ONLY_FAMILIES: Tuple[str, ...] = ("VESARR", "VESDEP")
#: Families that create vessel_call rows those events resolve against.
_CALL_PRODUCING_FAMILIES: Tuple[str, ...] = ("CALINF", "BERMAN", "JOURNAL_IN", "JOURNAL_OUT")

#: The sea-channel shapefile: a DIRECTORY of sidecar files, bundled below.
_SEA_CHANNEL_DIR = "2-JNPA_Sea_Channels_Bathymetry/Sea Channel"
#: Name given to the synthesised bundle in the ledger. Stable, so history reads sensibly.
_SEA_CHANNEL_ZIP = "JNPA_Sea_Channels.zip"

#: Berthing report PDFs - handled by their own subsystem.
_BERTHING_DIR = "7-Berthing Reports"


def _zip_bytes(paths: Sequence[Path]) -> bytes:
    """Bundle a shapefile's sidecars into a DETERMINISTIC ZIP.

    Byte-identical across runs and machines, which is what makes run 2 report
    SKIPPED_DUPLICATE: the ledger de-dupes on sha256 of these bytes, and Python's default
    ZipInfo would stamp the current mtime into every entry, changing the hash every time.

    Fixed here: entry order (sorted by name), timestamp (the ZIP epoch, 1980-01-01), and
    external attributes. Contents are the files' own bytes, unmodified.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(paths, key=lambda q: q.name.lower()):
            info = zipfile.ZipInfo(filename=p.name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            z.writestr(info, p.read_bytes())
    return buf.getvalue()


def discover(corpus: Path) -> List[Dict[str, Any]]:
    """The full ingest plan, in load order. Pure: reads the filesystem, nothing else."""
    plan: List[Dict[str, Any]] = []
    for family, rel, exts, recurse in _MARINE_FAMILIES:
        d = corpus / rel
        if not d.is_dir():
            continue
        it = sorted(d.rglob("*")) if recurse else sorted(d.iterdir())
        for p in it:
            if p.is_file() and p.suffix.lower() in exts:
                plan.append({"family": family, "path": p, "filename": p.name,
                             "content": None})

    shp_dir = corpus / _SEA_CHANNEL_DIR
    if shp_dir.is_dir():
        parts = [p for p in sorted(shp_dir.iterdir()) if p.is_file()]
        if parts:
            plan.append({"family": "SEA_CHANNEL", "path": shp_dir,
                         "filename": _SEA_CHANNEL_ZIP,
                         "content": _zip_bytes(parts)})
    return plan


# --------------------------------------------------------------------------- counts
#: What the loader reports on. Kept as (label, SQL) so adding a figure is one line and no
#: expected value is ever baked into this file.
_COUNT_QUERIES: Tuple[Tuple[str, str], ...] = (
    ("vessel_calls",        "SELECT count(*) FROM core.vessel_call"),
    ("vessels",             "SELECT count(*) FROM core.vessel"),
    ("vessel_call_events",  "SELECT count(*) FROM core.vessel_call_event"),
    ("pilotage",            "SELECT count(*) FROM core.pilotage"),
    ("pilots",              "SELECT count(*) FROM core.pilot"),
    ("port_craft",          "SELECT count(*) FROM core.port_craft"),
    ("sea_channel",         "SELECT count(*) FROM core.sea_channel"),
    ("berthing_records",    "SELECT count(*) FROM core.berthing_record"),
    ("berthing_terminals",  "SELECT count(DISTINCT terminal) FROM core.berthing_record"),
    ("marine_import_files", "SELECT count(*) FROM core.marine_import_files"),
    ("quarantined_errors",  "SELECT count(*) FROM core.marine_import_errors"),
)


async def measure(dsn: Optional[str]) -> Dict[str, Optional[int]]:
    """Row counts for the UC-I tables. A missing table reports None rather than raising,
    so a partially-migrated database still yields a usable report."""
    from sqlalchemy import text
    from jnpa_shared.db import get_engine

    from jnpa_shared.db import dispose_all

    out: Dict[str, Optional[int]] = {}
    engine = get_engine(dsn)
    for label, sql in _COUNT_QUERIES:
        # One connection PER query: a failed count aborts its transaction, and on a shared
        # connection every later count would then fail with InFailedSQLTransaction and be
        # reported as a missing table.
        try:
            async with engine.connect() as conn:
                out[label] = int((await conn.execute(text(sql))).scalar_one())
        except Exception:  # noqa: BLE001 - absent table is a reportable state
            out[label] = None
    await dispose_all()
    return out


#: Columns core.marine_import_errors actually has (migration 0045).
#:
#: The parser's ``error_code`` is NOT among them. VesselCallRepository._err_row flattens a
#: ParseResult error into this table's shape:
#:
#:     error_message := f"{column_name}: {error_detail or error_code}"
#:     raw_data      := raw_value
#:
#: so the code survives only when a finding carries no detail, and the column_name becomes
#: a prefix on the message. Grouping therefore uses that prefix as the nearest available
#: label, and the report says "label", not "code", because it is not one.
_QUARANTINE_SQL = """
SELECT f.filename                                        AS filename,
       CASE WHEN position(':' IN e.error_message) > 0
            THEN btrim(split_part(e.error_message, ':', 1))
            ELSE '(unlabelled)' END                      AS label,
       count(*)                                          AS n,
       min(e.row_number)                                 AS first_row,
       max(e.row_number)                                 AS last_row,
       min(e.error_message)                              AS sample,
       min(e.raw_data)                                   AS sample_raw
FROM core.marine_import_errors e
JOIN core.marine_import_files f ON f.id = e.import_file_id
GROUP BY 1, 2
ORDER BY n DESC, 1, 2
LIMIT :l
"""


async def quarantine_report(dsn: Optional[str], limit: int = 20) -> List[Dict[str, Any]]:
    """Data-quality findings held in core.marine_import_errors, busiest group first."""
    from sqlalchemy import text
    from jnpa_shared.db import dispose_all, get_engine

    try:
        async with get_engine(dsn).connect() as conn:
            rows = (await conn.execute(text(_QUARANTINE_SQL), {"l": limit})).mappings().all()
    except Exception:  # noqa: BLE001
        return []
    finally:
        await dispose_all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- run
async def ingest_marine(plan: List[Dict[str, Any]], dsn: Optional[str],
                        *, dry_run: bool) -> Dict[str, Any]:
    """Drive MarineUploadService over the plan. One file per call, statuses aggregated."""
    svc = None if dry_run else MarineUploadService(dsn)
    per_file: List[Dict[str, Any]] = []
    status_counts: Counter = Counter()
    totals = Counter()

    # jnpa_shared.db.get_engine() builds a NEW engine per call (the name is historical),
    # and VesselCallRepository calls it per operation. Over 51 files that is 51 pools left
    # holding connections, which is how a loader like this exhausts max_connections. The
    # phase disposes them on the way out; see the finally below.
    for item in plan:
        content = item["content"]
        if content is None:
            content = item["path"].read_bytes()
        name = item["filename"]

        if dry_run:
            # Parse only, through the very same entry point the import uses, so a dry run
            # exercises the real routing rather than an approximation of it.
            from services.marine.parsers import parse_marine
            res = parse_marine(content, name)
            row = {"family": item["family"], "filename": name,
                   "status": "REJECTED" if res.rejected else "PARSED",
                   "imported": 0, "skipped": 0, "failed": 0,
                   "records": len(res.records), "errors": len(res.errors),
                   "document_type": None}
        else:
            r = await svc.import_file(content, name, uploaded_by=UPLOADED_BY)
            row = {"family": item["family"], "filename": name, "status": r["status"],
                   "imported": r.get("imported", 0), "skipped": r.get("skipped", 0),
                   "failed": r.get("failed", 0),
                   "records": r.get("summary", {}).get("valid", 0),
                   "errors": len(r.get("errors", []) or []),
                   "document_type": r.get("document_type")}
            totals["imported"] += row["imported"]
            totals["skipped_rows"] += row["skipped"]
            totals["failed"] += row["failed"]

        totals["records"] += row["records"]
        totals["errors"] += row["errors"]
        status_counts[row["status"]] += 1
        per_file.append(row)

    if not dry_run:
        from jnpa_shared.db import dispose_all
        await dispose_all()

    return {"per_file": per_file, "status_counts": dict(status_counts),
            "totals": dict(totals)}


def ingest_berthing(corpus: Path, dsn: Optional[str], *, dry_run: bool) -> Dict[str, Any]:
    """Delegate the 25 terminal PDFs to the EXISTING berthing importer.

    scripts/import_berthing_reports.py already owns this corpus family: its own parser
    set, its own sha256 file ledger and its own upsert key. Re-implementing any of it here
    would be the duplicated ingestion path the ticket forbids, so this calls the two
    functions that script exposes and reports what they return.
    """
    d = corpus / _BERTHING_DIR
    if not d.is_dir():
        return {"files": 0, "rows": 0, "per_terminal": {}, "skipped_files": 0,
                "inserted": 0, "updated": 0, "note": f"absent: {d}"}

    from scripts.import_berthing_reports import collect, run_import

    files = collect(str(d))
    per_terminal = Counter()
    rows = 0
    for f in files:
        per_terminal[f["terminal"]] += len(f["records"])
        rows += len(f["records"])

    out: Dict[str, Any] = {"files": len(files), "rows": rows,
                           "per_terminal": dict(per_terminal)}
    if dry_run:
        out.update({"inserted": 0, "updated": 0, "skipped_files": 0, "dry_run": True})
        return out

    live = asyncio.run(run_import(files, dsn, ensure=True))
    out.update({"inserted": live.get("inserted", 0), "updated": live.get("updated", 0),
                "skipped_files": live.get("skipped_files", 0),
                "per_terminal_upserts": live.get("per_terminal", {})})
    return out


# --------------------------------------------------------------------------- reporting
def _parse_expect(values: Sequence[str]) -> Dict[str, int]:
    """--expect key=value ... , or --expect @path/to/file.json. Never a literal in code."""
    out: Dict[str, int] = {}
    for v in values:
        if v.startswith("@"):
            out.update({str(k): int(n) for k, n in json.loads(Path(v[1:]).read_text()).items()})
            continue
        if "=" not in v:
            raise SystemExit(f"--expect wants key=value or @file.json, got {v!r}")
        k, n = v.split("=", 1)
        out[k.strip()] = int(n)
    return out


def _print_report(marine: Dict[str, Any], berthing: Dict[str, Any],
                  before: Optional[Dict[str, Optional[int]]],
                  after: Optional[Dict[str, Optional[int]]],
                  expect: Dict[str, int], quarantine: List[Dict[str, Any]],
                  *, dry_run: bool, elapsed: float) -> bool:
    """Print the run report. Returns True when every --expect key matched."""
    bar = "=" * 78
    print("\n" + bar)
    print("UC1-002  UC-I CORPUS INGEST" + ("   [DRY-RUN - nothing written]" if dry_run else ""))
    print(bar)

    print("\n-- marine files (per family) " + "-" * 49)
    fam = Counter()
    fam_status: Dict[str, Counter] = {}
    for r in marine["per_file"]:
        fam[r["family"]] += 1
        fam_status.setdefault(r["family"], Counter())[r["status"]] += 1
    for f in sorted(fam):
        print(f"  {f:12} files={fam[f]:3}  {dict(fam_status[f])}")
    print(f"  {'TOTAL':12} files={len(marine['per_file']):3}  {marine['status_counts']}")

    t = marine["totals"]
    print(f"\n  records parsed : {t.get('records', 0)}")
    if not dry_run:
        print(f"  rows inserted  : {t.get('imported', 0)}")
        print(f"  rows skipped   : {t.get('skipped_rows', 0)}   (row-level duplicates)")
        print(f"  rows failed    : {t.get('failed', 0)}")
    print(f"  quarantined    : {t.get('errors', 0)}   (data-quality findings)")

    print("\n-- berthing reports " + "-" * 58)
    print(f"  files={berthing.get('files', 0)}  rows detected={berthing.get('rows', 0)}")
    print(f"  per terminal : {berthing.get('per_terminal', {})}")
    if not dry_run:
        print(f"  inserted={berthing.get('inserted', 0)}  updated={berthing.get('updated', 0)}"
              f"  duplicate files skipped={berthing.get('skipped_files', 0)}")

    ok = True
    if after is not None:
        print("\n-- database counts " + "-" * 59)
        head = f"  {'metric':22} {'before':>9} {'after':>9} {'delta':>9}"
        if expect:
            head += f" {'expected':>9}  verdict"
        print(head)
        for label, _ in _COUNT_QUERIES:
            b = (before or {}).get(label)
            a = after.get(label)
            delta = (a - b) if (isinstance(a, int) and isinstance(b, int)) else None
            line = (f"  {label:22} {('-' if b is None else b):>9} "
                    f"{('-' if a is None else a):>9} "
                    f"{('-' if delta is None else f'{delta:+d}'):>9}")
            if expect:
                e = expect.get(label)
                if e is None:
                    line += f" {'-':>9}"
                else:
                    good = (a == e)
                    ok = ok and good
                    line += f" {e:>9}  {'MATCH' if good else 'MISMATCH'}"
            print(line)

    if quarantine:
        print("\n-- quarantine (core.marine_import_errors) " + "-" * 36)
        for q in quarantine:
            rows = (f"rows {q['first_row']}-{q['last_row']}"
                    if q.get("first_row") is not None else "file-level")
            print(f"  {str(q['filename'])[:38]:38} {str(q['label'])[:20]:20} "
                  f"n={q['n']:<5} {rows}")
            print(f"      {str(q['sample'])[:150]}")
            if q.get("sample_raw"):
                print(f"      raw_data: {str(q['sample_raw'])[:100]}")

    print(f"\n  elapsed: {elapsed:.1f}s")
    print(bar + "\n")
    return ok


# --------------------------------------------------------------------------- main
def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", default=DEFAULT_CORPUS,
                    help=f"client-data root (default {DEFAULT_CORPUS})")
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN", "").strip() or None,
                    help="SQLAlchemy asyncpg DSN (defaults to $POSTGRES_DSN)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse everything, write nothing, need no database")
    ap.add_argument("--verify-only", action="store_true",
                    help="report database counts and quarantine, ingest nothing")
    ap.add_argument("--skip-berthing", action="store_true",
                    help="marine families only (berthing PDFs are slow to parse)")
    ap.add_argument("--expect", nargs="*", default=[], metavar="KEY=N",
                    help="expected counts, e.g. vessel_calls=660, or @expected.json. "
                         "Exit code 3 on any mismatch. Never hardcoded in this script.")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="also write the full report as JSON to this path")
    args = ap.parse_args(argv)

    corpus = Path(args.corpus).resolve()
    if not corpus.is_dir():
        raise SystemExit(f"corpus not found: {corpus}")
    if not args.dry_run and not args.dsn:
        raise SystemExit("no --dsn (and $POSTGRES_DSN is unset). Use --dry-run to parse "
                         "without a database.")
    expect = _parse_expect(args.expect)

    t0 = perf_counter()
    if args.verify_only:
        after = asyncio.run(measure(args.dsn))
        q = asyncio.run(quarantine_report(args.dsn))
        ok = _print_report({"per_file": [], "status_counts": {}, "totals": {}},
                           {"files": 0, "rows": 0, "per_terminal": {}},
                           None, after, expect, q, dry_run=False,
                           elapsed=perf_counter() - t0)
        return 0 if ok else 3

    plan = discover(corpus)
    if not plan:
        raise SystemExit(f"no marine corpus files found under {corpus}")
    print(f"==> corpus : {corpus}")
    print(f"==> plan   : {len(plan)} marine file(s)"
          + ("" if args.skip_berthing else " + berthing reports"))

    before = None if args.dry_run else asyncio.run(measure(args.dsn))
    marine = asyncio.run(ingest_marine(plan, args.dsn, dry_run=args.dry_run))
    berthing = ({"files": 0, "rows": 0, "per_terminal": {}, "note": "--skip-berthing"}
                if args.skip_berthing
                else ingest_berthing(corpus, args.dsn, dry_run=args.dry_run))
    after = None if args.dry_run else asyncio.run(measure(args.dsn))
    q = [] if args.dry_run else asyncio.run(quarantine_report(args.dsn))

    ok = _print_report(marine, berthing, before, after, expect, q,
                       dry_run=args.dry_run, elapsed=perf_counter() - t0)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"corpus": str(corpus), "marine": marine, "berthing": berthing,
             "counts_before": before, "counts_after": after,
             "expected": expect, "quarantine": q}, indent=2, default=str), encoding="utf-8")
        print(f"  report written: {args.json_out}\n")

    if expect and not ok:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
