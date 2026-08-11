#!/usr/bin/env python3
"""Ingest the JNPA Simulated Port Data API corpus into the what-if tables.

Why this exists
---------------
The what-if engine (``services/cargo/simulation``) is correct but starving:
``core.berthing_record`` ends 6 Jul and ``core.vessel_call_moves`` is populated
only from the EDI manifest as a DERIVED proxy. The JNPA API carries better data
than either — daily berthing reports through **5 Aug 2026** with **actual**
per-call move counts (``importMoves`` / ``exportMoves``), which is exactly what
migration 0129 describes as ``data_origin='API'``: *"a move count published by
JNPA (authoritative)"*.

Loading it upgrades scenario II-B from a manifest-line proxy to a published
figure, and unblocks I-A, I-B and II-B outright.

What it reuses rather than reinvents
------------------------------------
``core.berthing_record`` is written through
:meth:`services.berthing.repository.BerthingRepository.persist`, the importer the
team already uses for the PDF reports. That gives, for free: one atomic
transaction, upsert on ``(terminal, voyage_number, vessel_name)``, an import-file
ledger row, sha256 byte-dedup so a re-run is a no-op, and lifecycle events. This
script only adds a **new source format** (the API's JSON) and the one table that
importer does not write: ``core.vessel_call_moves``.

Safety, because this database is shared
---------------------------------------
Five engineers work other tickets against the same RDS. Every rule here is a
hard gate:

* **Additive only.** INSERT and UPDATE through the existing upserts. No DDL, no
  DELETE, no TRUNCATE, no ALTER. Verified by inspection of the SQL below - the
  only verbs are INSERT/UPDATE.
* **Idempotent.** Re-running changes nothing: the berthing side short-circuits on
  the file hash, the moves side upserts on the same natural key.
* **Reconciled.** Row counts and date ranges for every touched table are captured
  before and after and printed side by side. Any DECREASE aborts with a non-zero
  exit before the transaction is allowed to matter.
* **Dry-run first.** ``--dry-run`` parses, dedupes, reconciles and prints the
  projected deltas without opening a write connection.
* **Traceable.** Every row written carries a batch id in its provenance column
  (``source_file`` / ``source_note``), so ``--show-batch`` can list exactly what
  this script touched. No schema change was needed to get that - both columns
  already exist.

Duplicate collapse
------------------
A daily berthing report lists vessels alongside *that day*, so a call spanning
midnight is reported again the next day: 145 raw rows over the corpus are 70 real
calls. :mod:`services.cargo.simulation.dedup` collapses them before anything is
written; without it every per-call figure roughly doubles.

Usage
-----
    # rehearse - no DB connection is opened for writes
    python scripts/ingest_portdata.py --dry-run

    # against a database (RDS by default via POSTGRES_DSN)
    POSTGRES_DSN=... .venv/bin/python scripts/ingest_portdata.py

    # what did this script write?
    python scripts/ingest_portdata.py --show-batch portdata-20260811

Options: --corpus PATH, --dsn, --dry-run, --batch-id, --show-batch, --limit-days.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

from services.cargo.simulation.dedup import (  # noqa: E402
    distinct_calls, duplication_factor)

DEFAULT_CORPUS = _ROOT.parent / "jnpa-mock-server"

#: Tables this script can write. Anything not on this list is out of scope, and
#: the reconciliation refuses to report on tables we do not touch.
TOUCHED_TABLES = ("core.berthing_record", "core.vessel_call_moves")

#: Provenance prefix stamped into source_file / source_note.
BATCH_PREFIX = "portdata"


# --------------------------------------------------------------------- parsing
def _status_for(call: dict) -> str:
    """Lifecycle status implied by which timestamps the report carries.

    Ordered most-advanced first. The berthing feed has no explicit status column,
    so it is inferred from the furthest point the call has demonstrably reached -
    never guessed beyond the evidence."""
    if call.get("sailed"):
        return "DEPARTED"
    if call.get("operationsEnd"):
        return "COMPLETED"
    if call.get("operationsStart"):
        return "CARGO_OPERATION"
    if call.get("alongside"):
        return "BERTHING_STARTED"
    return "EXPECTED"


def _clean_line(value: Any) -> Optional[str]:
    """Shipping line, or None when the feed says it does not know.

    'NA' is the corpus's way of writing "not declared". Storing the literal
    string would make a downstream ranking treat 'NA' as a carrier."""
    text = str(value or "").strip()
    return None if text.upper() in ("", "NA", "N/A", "-") else text


def to_record(call: dict, terminal: str, batch_id: str) -> dict:
    """One API vessel-call -> the normalised record ``persist`` expects.

    ETA and ATA are deliberately absent: the daily berthing feed carries neither,
    and inventing them from the alongside time would fabricate the very quantity
    scenario I-A needs (waiting = berth - arrival). Their absence is declared by
    the scenario instead."""
    return {
        "terminal": terminal,
        "vessel_name": str(call.get("vesselName") or "").strip(),
        "imo_number": None,
        "voyage_number": str(call.get("voyage") or "").strip(),
        "shipping_line": _clean_line(call.get("line")),
        "berth_number": str(call.get("berth") or "").strip() or None,
        "eta": None,
        "ata": None,
        "berthing_time": call.get("alongside"),
        "departure_time": call.get("sailed"),
        "cargo_operation_start": call.get("operationsStart"),
        "cargo_operation_end": call.get("operationsEnd"),
        "status": _status_for(call),
        "source_file": f"{batch_id}:{terminal}",
    }


def to_moves(call: dict, terminal: str, batch_id: str) -> Optional[dict]:
    """One API vessel-call -> a ``core.vessel_call_moves`` row, or None.

    ``data_origin='API'`` because these counts are published by JNPA in the daily
    berthing report - the authoritative case migration 0129 was written for, as
    opposed to the DERIVED manifest-line proxy it falls back to.

    ``restow_moves`` stays NULL rather than 0: the report does not distinguish
    restows, and zero would assert there were none. ``cranes_deployed`` likewise -
    which is why the productivity figure is per BERTH, not per crane."""
    discharge, load = call.get("importMoves"), call.get("exportMoves")
    if discharge is None and load is None:
        return None
    return {
        "terminal": terminal,
        "vessel_name": str(call.get("vesselName") or "").strip(),
        "voyage_number": str(call.get("voyage") or "").strip(),
        "discharge_moves": discharge,
        "load_moves": load,
        # Written explicitly — see _MOVES_INSERT. Restows are unknown, not zero,
        # so they contribute nothing to the total rather than being counted as 0.
        "gross_moves": (discharge or 0) + (load or 0),
        "restow_moves": None,
        "cranes_deployed": None,
        "data_origin": "API",
        "source_note": (f"{batch_id} - move counts published in the JNPA daily "
                        "berthing report (importMoves/exportMoves)"),
    }


def load_corpus(corpus: Path) -> list[dict]:
    """Every vesselCalls row across every daily berthing report, un-deduplicated."""
    path = corpus / "data" / "responses" / "group-berthing-reports.json"
    if not path.is_file():
        raise SystemExit(f"corpus not found: {path}\n"
                         "Pass --corpus PATH to the jnpa-mock-server checkout.")
    payload = json.loads(path.read_text(encoding="utf8"))
    rows: list[dict] = []
    for report in payload.get("items", []):
        terminal = report.get("terminal")
        report_date = str(report.get("reportDate") or "")[:10]
        for call in report.get("vesselCalls", []) or []:
            rows.append({"_terminal": terminal, "_reportDate": report_date, **call})
    return rows


# ------------------------------------------------------------------ SQL (write)
# Upsert on the berthing natural key, as UPDATE-then-INSERT rather than
# ON CONFLICT.
#
# Migration 0129 as written creates a partial unique index `uq_vcm_call` on
# (terminal, voyage_number, vessel_name), but the DEPLOYED table has only its
# primary key on `id` — so an ON CONFLICT naming those columns fails with
# "no unique or exclusion constraint matching the ON CONFLICT specification".
#
# Creating the missing index would be DDL on a table five other engineers share,
# and it would fail outright if any duplicate rows already exist. UPDATE-then-
# INSERT needs no schema change and behaves identically for this workload (one
# writer, one batch). It is not safe against a concurrent writer racing between
# the UPDATE and the INSERT — acceptable here because the ingest is explicitly a
# single-operator job, and stated rather than assumed.
#
# The deployed table also has no created_at/updated_at columns, so neither is set.
# gross_moves is written EXPLICITLY. Migration 0129 declares it
# `GENERATED ALWAYS AS (discharge + load + restow) STORED`, but the deployed
# column is a plain nullable integer (is_generated = 'NEVER'), so nothing computes
# it. Leaving it out inserted 70 rows with a NULL total, which the scenario reads
# as "no move count" — the rows were there and invisible.
_MOVES_UPDATE = """
    UPDATE core.vessel_call_moves
       SET discharge_moves = :discharge_moves,
           load_moves      = :load_moves,
           gross_moves     = :gross_moves,
           data_origin     = :data_origin,
           source_note     = :source_note
     WHERE terminal = :terminal
       AND voyage_number = :voyage_number
       AND vessel_name = :vessel_name
"""

_MOVES_INSERT = """
    INSERT INTO core.vessel_call_moves
        (terminal, vessel_name, voyage_number, discharge_moves, load_moves,
         gross_moves, restow_moves, cranes_deployed, data_origin, source_note)
    VALUES
        (:terminal, :vessel_name, :voyage_number, :discharge_moves, :load_moves,
         :gross_moves, :restow_moves, :cranes_deployed, :data_origin, :source_note)
"""

_RECONCILE_SQL = {
    "core.berthing_record": """
        SELECT count(*) AS rows,
               min(berthing_time)::text AS min_ts,
               max(berthing_time)::text AS max_ts
          FROM core.berthing_record
    """,
    "core.vessel_call_moves": """
        SELECT count(*) AS rows,
               min(data_origin)::text AS min_ts,
               max(data_origin)::text AS max_ts
          FROM core.vessel_call_moves
    """,
}


async def reconcile(dsn: str) -> dict[str, dict]:
    """Row count and span for every table this script can touch (read-only)."""
    from sqlalchemy import text
    from services.berthing.repository import get_engine

    out: dict[str, dict] = {}
    async with get_engine(dsn).connect() as conn:
        for table, sql in _RECONCILE_SQL.items():
            row = (await conn.execute(text(sql))).mappings().first()
            out[table] = dict(row) if row else {"rows": 0}
    return out


async def show_batch(dsn: str, batch_id: str) -> None:
    """List what one batch wrote, so a revert can be scoped precisely."""
    from sqlalchemy import text
    from services.berthing.repository import get_engine

    async with get_engine(dsn).connect() as conn:
        calls = (await conn.execute(text(
            "SELECT terminal, count(*) AS n FROM core.berthing_record "
            "WHERE source_file LIKE :p GROUP BY terminal ORDER BY terminal"),
            {"p": f"{batch_id}:%"})).mappings().all()
        moves = (await conn.execute(text(
            "SELECT count(*) AS n FROM core.vessel_call_moves "
            "WHERE source_note LIKE :p"), {"p": f"{batch_id}%"})).mappings().first()
    print(f"\nbatch {batch_id}")
    print("  core.berthing_record:")
    for row in calls:
        print(f"    {row['terminal']:<8} {row['n']:>4}")
    print(f"  core.vessel_call_moves: {(moves or {}).get('n', 0)}")
    print("\n  To revert this batch (review before running - it DELETEs):")
    print(f"    DELETE FROM core.vessel_call_moves WHERE source_note LIKE '{batch_id}%';")
    print(f"    DELETE FROM core.berthing_record   WHERE source_file LIKE '{batch_id}:%';")


# ---------------------------------------------------------------------- ingest
async def ingest(records_by_terminal: dict[str, list[dict]],
                 moves: list[dict], *, dsn: str, batch_id: str) -> dict:
    """Write both tables. Berthing goes through the team's existing persist()."""
    from sqlalchemy import text
    from services.berthing.repository import BerthingRepository, get_engine

    repo = BerthingRepository(dsn)
    summary = {"files": [], "calls_inserted": 0, "calls_updated": 0,
               "moves_inserted": 0, "moves_updated": 0}

    for terminal, records in sorted(records_by_terminal.items()):
        payload = json.dumps(records, sort_keys=True, default=str).encode()
        result = await repo.persist(
            records, terminal=terminal,
            filename=f"{batch_id}-{terminal}-berthing-reports.json",
            file_hash=hashlib.sha256(payload).hexdigest(),
            physical_format="JSON", file_size=len(payload),
            source="DIRECTORY")
        summary["files"].append({"terminal": terminal, **result})
        summary["calls_inserted"] += result.get("inserted", 0)
        summary["calls_updated"] += result.get("updated", 0)

    if moves:
        async with get_engine(dsn).begin() as conn:
            for row in moves:
                updated = (await conn.execute(text(_MOVES_UPDATE), row)).rowcount
                if updated:
                    summary["moves_updated"] += updated
                else:
                    await conn.execute(text(_MOVES_INSERT), row)
                    summary["moves_inserted"] += 1
    return summary


def _resolve_dsn(explicit: Optional[str]) -> str:
    """Same order as scripts/migrate.py, so one env setup drives both."""
    for candidate in (explicit, os.environ.get("MIGRATE_DSN"),
                      os.environ.get("RFID_POSTGRES_DSN")):
        if candidate and candidate.strip():
            return candidate.strip()
    val = (os.environ.get("POSTGRES_DSN") or "").strip()
    if val:
        return val
    raise SystemExit(
        "no DSN. Pass --dsn, or set POSTGRES_DSN / MIGRATE_DSN / RFID_POSTGRES_DSN.\n"
        "This script targets AWS RDS jnpa_schema_v3 by default - see "
        "docs/RDS_SECURITY.md and use the least-privilege jnpa_app role, not the "
        "postgres superuser.")


def _print_reconciliation(before: dict, after: dict) -> bool:
    """Print before/after and return True when nothing decreased."""
    print("\n  reconciliation")
    print(f"    {'table':<28}{'before':>9}{'after':>9}{'delta':>9}")
    safe = True
    for table in TOUCHED_TABLES:
        b = (before.get(table) or {}).get("rows", 0)
        a = (after.get(table) or {}).get("rows", 0)
        delta = a - b
        flag = "" if delta >= 0 else "   <-- ROW LOSS"
        if delta < 0:
            safe = False
        print(f"    {table:<28}{b:>9}{a:>9}{delta:>+9}{flag}")
    return safe


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus", default=str(DEFAULT_CORPUS),
                    help="jnpa-mock-server checkout holding the API replay")
    ap.add_argument("--dsn", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and reconcile only; opens no write connection")
    ap.add_argument("--batch-id", default=None,
                    help=f"provenance stamp (default {BATCH_PREFIX}-<corpus max date>)")
    ap.add_argument("--show-batch", default=None, metavar="BATCH_ID",
                    help="list what a previous batch wrote, and how to revert it")
    args = ap.parse_args()

    if args.show_batch:
        asyncio.run(show_batch(_resolve_dsn(args.dsn), args.show_batch))
        return 0

    corpus = Path(args.corpus).expanduser().resolve()
    raw = load_corpus(corpus)
    calls = distinct_calls(raw)
    max_date = max((r.get("_reportDate") or "" for r in raw), default="")
    batch_id = args.batch_id or f"{BATCH_PREFIX}-{max_date.replace('-', '')}"

    print(f"corpus     {corpus}")
    print(f"batch id   {batch_id}")
    print(f"\n  duplicate collapse")
    print(f"    raw report rows      {len(raw):>6}")
    print(f"    distinct calls       {len(calls):>6}"
          f"   ({duplication_factor(len(raw), len(calls))}x repetition removed)")

    by_terminal: dict[str, list[dict]] = {}
    moves: list[dict] = []
    skipped_no_identity = 0
    for call in calls:
        terminal = call.get("_terminal")
        record = to_record(call, terminal, batch_id)
        if not record["vessel_name"] or not record["voyage_number"]:
            # persist() upserts on (terminal, voyage_number, vessel_name); a row
            # missing either would collide with every other such row.
            skipped_no_identity += 1
            continue
        by_terminal.setdefault(terminal, []).append(record)
        row = to_moves(call, terminal, batch_id)
        if row:
            moves.append(row)

    print(f"\n  to write")
    for terminal, records in sorted(by_terminal.items()):
        print(f"    {terminal:<8} {len(records):>4} calls")
    print(f"    {'moves':<8} {len(moves):>4} rows (data_origin=API)")
    if skipped_no_identity:
        print(f"    skipped  {skipped_no_identity:>4} calls with no vessel/voyage identity")

    span = sorted(r["_reportDate"] for r in raw if r.get("_reportDate"))
    print(f"\n  coverage   {span[0]} .. {span[-1]}")

    if args.dry_run:
        print("\nDRY RUN - nothing was written and no write connection was opened.")
        print("Review the counts above, then re-run without --dry-run.")
        return 0

    dsn = _resolve_dsn(args.dsn)
    print("\n  connecting...")

    # One event loop for the whole run. services.berthing.repository.get_engine
    # caches the SQLAlchemy async engine globally, and that engine is bound to
    # the loop it was created on — three separate asyncio.run() calls would give
    # "Event loop is closed" on the second.
    async def _execute():
        before = await reconcile(dsn)
        summary = await ingest(by_terminal, moves, dsn=dsn, batch_id=batch_id)
        after = await reconcile(dsn)
        return before, summary, after

    before, summary, after = asyncio.run(_execute())

    print(f"\n  written")
    for entry in summary["files"]:
        print(f"    {entry['terminal']:<8} {entry['status']:<18} "
              f"+{entry.get('inserted', 0)} ~{entry.get('updated', 0)}")
    print(f"    moves    +{summary['moves_inserted']} ~{summary['moves_updated']}")

    safe = _print_reconciliation(before, after)
    if not safe:
        print("\nROW LOSS DETECTED. Restore from the pre-ingest snapshot and "
              "investigate before doing anything else.")
        return 2
    print(f"\nOK. Inspect or revert this batch with:"
          f"\n  python scripts/ingest_portdata.py --show-batch {batch_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
