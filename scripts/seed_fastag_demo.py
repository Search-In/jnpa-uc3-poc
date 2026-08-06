#!/usr/bin/env python3
"""Seed realistic FASTag operational data for the 10 demo vehicles.

WHAT THIS FIXES
---------------
Before this script, ``core.fastag_balance`` held three rows that were all the
same vehicle-agnostic stub (``DEMOFASTAG001`` / ``DEMO_BANK`` / ``850.00``),
``core.fastag_transaction`` held eight crossings for a plate nobody searches, and
``core.toll_enroute`` was empty. Searching any of the demo plates on the FASTag
screen produced empty Transactions / Journey / History cards.

WHAT IT WRITES
--------------
Per vehicle in :data:`services.fastag.demo_dataset.SEED_PLATES`:

  * ``core.fastag_balance``      — one ACTIVE account (tag id, issuer, ₹500-₹5000)
  * ``core.fastag_transaction``  — 5-10 SUCCESS crossings over the last 30 days
  * ``core.toll_enroute``        — one row per completed trip (origin, destination,
                                   distance, duration, plazas + fares)

SEEDED THROUGH THE REAL PIPELINE
--------------------------------
Rows go in via ``mapper -> FastagService``, never raw INSERTs — the same path
``POST /api/fastag/*`` uses. So the seed exercises the live contract (a DTO or
column drift fails *here*, loudly), money stays Decimal, timestamps stay
tz-aware UTC, and dedup is the production ``ON CONFLICT (seq_no)`` rule rather
than something this script reimplements.

The payloads come from :mod:`services.fastag.demo_dataset`, which is also what
``FASTAG_DEMO_MODE`` serves — so a live demo fetch and this persisted history are
the same data, not two drifting fixtures.

IDEMPOTENT
----------
  * balance      — UPSERT on rc_number
  * transactions — ``ON CONFLICT (seq_no) DO NOTHING``; ``seq_no`` is derived from
                   the plate and the crossing ordinal (never its timestamp), so a
                   re-run inserts 0 no matter how much later it happens
  * toll_enroute — has no dedup key, so seeded legs (marked
                   ``client_id = 'demo-seed:<RC>'``) are cleared for the vehicle
                   before being rewritten. Real rows carry a correlation UUID and
                   are never touched.

USAGE
-----
    python scripts/seed_fastag_demo.py                  # uses $POSTGRES_DSN
    python scripts/seed_fastag_demo.py --dsn "..."      # explicit target
    python scripts/seed_fastag_demo.py --dry-run        # show the plan, write nothing
    python scripts/seed_fastag_demo.py --verify         # report what is in the DB
    python scripts/seed_fastag_demo.py --purge          # remove seeded rows

No schema change and no API change is required or implied.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (REPO_ROOT, REPO_ROOT / "shared"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from services.fastag import demo_dataset as ds  # noqa: E402
from services.fastag.demo_provider import (  # noqa: E402
    demo_balance,
    demo_transactions,
)
from services.fastag.mappers import (  # noqa: E402
    map_fastag_balance,
    map_fastag_transactions,
    map_toll_enroute,
)
from services.fastag.service import FastagService  # noqa: E402

#: Marks a journey row as seeded, so --purge and the idempotency sweep can find
#: it without risking an operator's real toll-enroute lookups.
SEED_CLIENT_PREFIX = "demo-seed:"


# --------------------------------------------------------------------------- env
def load_env_file(path: Path) -> None:
    """Read a ``KEY=VALUE`` env file without clobbering the real environment."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def resolve_dsn(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    load_env_file(REPO_ROOT / ".env.local")
    dsn = os.environ.get("POSTGRES_DSN", "").strip()
    if not dsn:
        raise SystemExit(
            "POSTGRES_DSN is not set. Pass --dsn, or fill it in .env.local "
            "(see .env.local.example)."
        )
    return dsn


# ----------------------------------------------------------------------- seeding
async def seed_vehicle(service: FastagService, rc: str, dsn: str) -> dict[str, Any]:
    """Seed one vehicle's account, crossings and trips. Returns a per-RC summary."""
    from jnpa_shared.db import execute

    cid = f"seed-fastag-{rc}"
    summary: dict[str, Any] = {"rc_number": rc}

    # 1) Account snapshot -> core.fastag_balance (UPSERT).
    mapped = map_fastag_balance(demo_balance(rc), client_id=cid)
    _require(mapped, "balance", rc)
    result = await service.process_balance(mapped, client_id=cid)
    _require_service(result, "balance", rc)
    summary["tag_id"] = result.get("tag_id")
    summary["balance"] = result.get("available_balance")
    summary["tag_status"] = result.get("tag_status")

    # 2) Toll crossings -> core.fastag_transaction (dedup on seq_no).
    mapped = map_fastag_transactions(demo_transactions(rc), client_id=cid)
    _require(mapped, "transactions", rc)
    result = await service.process_transactions(mapped, client_id=cid)
    _require_service(result, "transactions", rc)
    summary["txn_total"] = result.get("total")
    summary["txn_inserted"] = result.get("inserted_count")
    summary["txn_skipped"] = result.get("skipped_count")

    # 3) Completed trips -> core.toll_enroute. The table has no dedup key, so
    #    clear this vehicle's previously-seeded legs first (real rows, whose
    #    client_id is a correlation UUID, cannot match the prefix).
    await execute(
        "DELETE FROM core.toll_enroute WHERE client_id = :cid",
        {"cid": f"{SEED_CLIENT_PREFIX}{rc}"},
        dsn=dsn,
    )
    journeys = ds.journeys_payload(rc)
    summary["journeys"] = 0
    summary["tolls_crossed"] = 0
    for journey in journeys:
        # Strip the derived, non-column extras before the mapper sees them.
        payload = {k: v for k, v in journey.items() if not k.startswith("_")}
        mapped = map_toll_enroute(payload, client_id=journey["client_id"])
        _require(mapped, "toll_enroute", rc)
        result = await service.process_toll_enroute(mapped, client_id=journey["client_id"])
        _require_service(result, "toll_enroute", rc)
        # created_at defaults to now(); backdate it to the trip so the journey
        # history reads as a month of running, not one bulk import.
        await execute(
            "UPDATE core.toll_enroute SET created_at = :ts WHERE id = CAST(:id AS uuid)",
            {"ts": journey["_started_at"], "id": result["id"]},
            dsn=dsn,
        )
        summary["journeys"] += 1
        summary["tolls_crossed"] += journey["_tolls_crossed"]

    summary["journeys_completed"] = sum(1 for j in journeys if j["_completed"])
    return summary


def _require(mapped: dict, stage: str, rc: str) -> None:
    if mapped.get("status") != "success":
        raise SystemExit(f"[{rc}] {stage} mapper rejected the payload: {mapped.get('reason')}")


def _require_service(result: dict, stage: str, rc: str) -> None:
    if result.get("status") != "SUCCESS":
        raise SystemExit(f"[{rc}] {stage} persist failed: {result.get('reason')}")


async def run_seed(dsn: str, plates: tuple[str, ...]) -> list[dict[str, Any]]:
    service = FastagService(dsn=dsn)
    rows = []
    for rc in plates:
        rows.append(await seed_vehicle(service, rc, dsn))
    return rows


# ------------------------------------------------------------------- purge/verify
async def run_purge(dsn: str, plates: tuple[str, ...]) -> dict[str, int]:
    from jnpa_shared.db import execute

    seeded_clients = [f"{SEED_CLIENT_PREFIX}{rc}" for rc in plates]
    return {
        "toll_enroute": await execute(
            "DELETE FROM core.toll_enroute WHERE client_id = ANY(:cids)",
            {"cids": seeded_clients}, dsn=dsn,
        ),
        "fastag_transaction": await execute(
            "DELETE FROM core.fastag_transaction WHERE rc_number = ANY(:rcs)",
            {"rcs": list(plates)}, dsn=dsn,
        ),
        "fastag_balance": await execute(
            "DELETE FROM core.fastag_balance WHERE rc_number = ANY(:rcs)",
            {"rcs": list(plates)}, dsn=dsn,
        ),
    }


async def run_verify(dsn: str, plates: tuple[str, ...]) -> list[dict[str, Any]]:
    from jnpa_shared.db import fetch_all

    rows = await fetch_all(
        """
        SELECT b.rc_number,
               b.tag_id,
               b.provider_name,
               b.available_balance,
               b.tag_status,
               (SELECT count(*) FROM core.fastag_transaction t
                 WHERE t.rc_number = b.rc_number)                       AS txns,
               (SELECT max(t.transaction_date_time) FROM core.fastag_transaction t
                 WHERE t.rc_number = b.rc_number)                       AS last_seen,
               (SELECT count(*) FROM core.toll_enroute e
                 WHERE e.client_id = :prefix || b.rc_number)            AS journeys
          FROM core.fastag_balance b
         WHERE b.rc_number = ANY(:rcs)
         ORDER BY b.rc_number
        """,
        {"rcs": list(plates), "prefix": SEED_CLIENT_PREFIX},
        dsn=dsn,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------- reporting
def print_plan(plates: tuple[str, ...]) -> None:
    print(f"Plan — {len(plates)} vehicles (nothing will be written):\n")
    hdr = f"{'RC':<12} {'BALANCE':>10}  {'TAG ID':<26} {'TXNS':>4} {'TRIPS':>5}  ROUTE"
    print(hdr)
    print("-" * len(hdr))
    for rc in plates:
        p = ds.profile_for(rc)
        trips = ds.trips_for(rc)
        n = sum(t.tolls_crossed for t in trips)
        print(
            f"{rc:<12} {'Rs ' + str(ds.balance_for(rc)):>10}  {ds.tag_id_for(rc):<26} "
            f"{n:>4} {len(trips):>5}  {ds.SOURCE_NAME} <-> {p.corridor.dest_name} "
            f"({p.corridor.distance_km} km, {len(p.corridor.plaza_keys)} plazas)"
        )


def print_seeded(rows: list[dict[str, Any]]) -> None:
    hdr = (f"{'RC':<12} {'BALANCE':>10} {'STATUS':<10} {'TXN':>4} {'NEW':>4} "
           f"{'DUP':>4} {'TRIPS':>5} {'DONE':>5} {'TOLLS':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(
            f"{r['rc_number']:<12} {'Rs ' + str(r['balance']):>10} {str(r['tag_status']):<10} "
            f"{r['txn_total']:>4} {r['txn_inserted']:>4} {r['txn_skipped']:>4} "
            f"{r['journeys']:>5} {r['journeys_completed']:>5} {r['tolls_crossed']:>6}"
        )


def print_verify(rows: list[dict[str, Any]], plates: tuple[str, ...]) -> bool:
    hdr = (f"{'RC':<12} {'BALANCE':>10} {'STATUS':<10} {'TXNS':>5} {'TRIPS':>5}  "
           f"{'LAST SEEN (UTC)':<20} PROVIDER")
    print(hdr)
    print("-" * len(hdr))
    seen = {r["rc_number"] for r in rows}
    ok = True
    for r in rows:
        last = r["last_seen"].strftime("%Y-%m-%d %H:%M") if r["last_seen"] else "-"
        print(
            f"{r['rc_number']:<12} {'Rs ' + str(r['available_balance']):>10} "
            f"{str(r['tag_status']):<10} {r['txns']:>5} {r['journeys']:>5}  {last:<20} "
            f"{r['provider_name']}"
        )
        if r["txns"] < ds.TXN_MIN or r["journeys"] < 1 or not r["available_balance"]:
            ok = False
    for rc in plates:
        if rc not in seen:
            print(f"{rc:<12} MISSING — no balance row")
            ok = False
    return ok


# --------------------------------------------------------------------------- main
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", help="Postgres DSN (default: $POSTGRES_DSN / .env.local)")
    ap.add_argument("--dry-run", action="store_true", help="show the plan, write nothing")
    ap.add_argument("--verify", action="store_true", help="report what is in the DB and exit")
    ap.add_argument("--purge", action="store_true", help="delete the seeded rows and exit")
    ap.add_argument("--rc", action="append", metavar="PLATE",
                    help="limit to these plates (repeatable; default: all 10)")
    args = ap.parse_args(argv)

    plates: tuple[str, ...] = tuple(r.upper() for r in args.rc) if args.rc else ds.SEED_PLATES

    if args.dry_run:
        print_plan(plates)
        return 0

    dsn = resolve_dsn(args.dsn)

    if args.purge:
        deleted = asyncio.run(run_purge(dsn, plates))
        for table, n in deleted.items():
            print(f"deleted {n:>4} rows from core.{table}")
        return 0

    if args.verify:
        rows = asyncio.run(run_verify(dsn, plates))
        ok = print_verify(rows, plates)
        print("\nOK — every vehicle has a balance, crossings and trips."
              if ok else "\nINCOMPLETE — run without --verify to seed.")
        return 0 if ok else 1

    print(f"Seeding FASTag demo data for {len(plates)} vehicles...\n")
    rows = asyncio.run(run_seed(dsn, plates))
    print_seeded(rows)
    print(f"\nSeeded {len(rows)} vehicles: "
          f"{sum(r['txn_total'] for r in rows)} crossings, "
          f"{sum(r['journeys'] for r in rows)} trips.")
    print("Verify with: python scripts/seed_fastag_demo.py --verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
