#!/usr/bin/env python3
"""Deterministic seeder for the vehicle -> transporter registry (UC3-004).

Gap G6: the customer's master data has no vehicle numbers. TransporterDetails
.xlsx lists companies, PDP Details.xlsx lists drivers and permits, and NEITHER
carries a plate. The only place the vehicle->transporter relationship is written
down is the gate-document corpus, and only on the slips that actually print a
transporter name.

So the registry is MIXED by necessity, and this script keeps the two halves
strictly apart:

  DOCUMENT_EVIDENCED  read from core.gate_document where data_origin='REAL' and
                      transporter_name is present. source_ref records the
                      doc_variant, so every claim traces back to a physical slip.

  SYNTHETIC           generated for a plate the corpus does not evidence.
                      assumption_ref='A-G6' on every one, enforced by the CHECK
                      added in migration 0134 — a synthetic row physically
                      cannot be stored without its assumption.

Nothing is invented into the DOCUMENT_EVIDENCED half. A printed transporter name
that does not resolve to exactly ONE row of core.transporter is reported as
unresolved and skipped, never guessed at.

Determinism: the synthetic transporter for a plate is chosen by
sha256(seed || plate) modulo the sorted list of real transporter ids. No RNG
state, no wall-clock, no dict ordering — the same seed and the same database
produce byte-identical assignments on any machine, and a re-run updates in place
rather than inserting a second row (ON CONFLICT on the existing
UNIQUE(transporter_id, vehicle_no_norm)).

Usage:
    UC3_TARGET_DB=jnpa_schema_v3 POSTGRES_DSN=... \\
        ./.venv/bin/python scripts/seed_uc3_004_vehicle_registry.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

#: Plates the ticket requires the registry to resolve. Membership here does NOT
#: make a mapping real — evidence decides that; this is only the work list.
CONTRACT_PLATES: Tuple[str, ...] = (
    "MH43BX1488", "MH43CQ2814", "MH43CQ2732", "MH43CQ2731", "MH43CQ0554",
    "MH43CK1959", "MH46AF4375", "MH43U7042", "MH46CU9869", "MH46H6948",
)

#: Frozen so the generated half is reproducible. Changing it re-assigns every
#: SYNTHETIC row, which is why it is a constant and not a CLI default.
SEED = "UC3-004:A-G6:v1"

ASSUMPTION_REF = "A-G6"


class SeedError(RuntimeError):
    """Refuse to write rather than write something wrong."""


def norm_plate(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def norm_company(name: str) -> str:
    """Fold to letters+digits so 'Transtar Handling & Warehousing Co' and
    'TRANSTAR HANDLING & WAREHOUSING CO' compare equal."""
    return re.sub(r"[^A-Z0-9]", "", (name or "").upper())


def resolve_company(printed: str, companies: Sequence[Tuple[int, str]]) -> Optional[int]:
    """Map a transporter name printed on a slip to exactly one core.transporter.

    The corpus truncates: one EIR prints 'TRANSTA' for what the master data calls
    'TRANSTAR HANDLING & WAREHOUSING CO' (the same truncation the DQ ledger
    already records as truncated_value). So an exact match is tried first, then a
    prefix match — but a prefix that hits more than one company is AMBIGUOUS and
    returns None rather than picking one.
    """
    key = norm_company(printed)
    if not key:
        return None
    exact = [cid for cid, name in companies if norm_company(name) == key]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None  # genuinely ambiguous in the master data; do not guess
    prefix = [cid for cid, name in companies if norm_company(name).startswith(key)]
    return prefix[0] if len(prefix) == 1 else None


def synthetic_pick(plate: str, transporter_ids: Sequence[int]) -> int:
    """Deterministic, machine-independent choice of a filler transporter."""
    if not transporter_ids:
        raise SeedError("core.transporter is empty — cannot generate mappings")
    digest = hashlib.sha256(f"{SEED}|{plate}".encode("utf-8")).hexdigest()
    return transporter_ids[int(digest, 16) % len(transporter_ids)]


async def run(dsn: str, dry_run: bool) -> int:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    stats = {"evidenced": 0, "synthetic": 0, "unresolved": 0, "written": 0}
    rows: List[Dict[str, object]] = []

    async with engine.begin() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar()
        expected = (os.environ.get("UC3_TARGET_DB") or "jnpa_qa").strip()
        if db != expected:
            raise SeedError(
                f"refusing to write: connected to database {db!r}, expected {expected!r}. "
                f"Set UC3_TARGET_DB to the database you intend to write to.")

        cols = (await conn.execute(text(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='core' AND table_name='transporter_vehicle' "
            "AND column_name IN ('provenance','assumption_ref','source_ref')"))).scalar()
        if cols != 3:
            raise SeedError("migration 0134 not applied — provenance columns missing")

        companies: List[Tuple[int, str]] = [
            (int(r[0]), r[1] or "") for r in (await conn.execute(text(
                "SELECT id, company_name FROM core.transporter ORDER BY id"))).all()
        ]
        if not companies:
            raise SeedError("core.transporter is empty — load UC3-001 first")
        transporter_ids = sorted(cid for cid, _ in companies)

        # --- the evidenced half: straight off the REAL gate documents ---------
        evidence = (await conn.execute(text(
            "SELECT DISTINCT vehicle_no, transporter_name, doc_variant "
            "FROM core.gate_document "
            "WHERE data_origin = 'REAL' AND vehicle_no IS NOT NULL "
            "  AND transporter_name IS NOT NULL AND btrim(transporter_name) <> '' "
            "ORDER BY vehicle_no, doc_variant"))).all()

        seen: set = set()
        for vehicle_no, printed, doc_variant in evidence:
            plate = norm_plate(vehicle_no)
            if plate in seen:
                continue
            cid = resolve_company(printed, companies)
            if cid is None:
                stats["unresolved"] += 1
                print(f"  UNRESOLVED  {plate:<12} printed={printed!r} "
                      f"(no unique core.transporter match) — skipped, not guessed")
                continue
            seen.add(plate)
            stats["evidenced"] += 1
            rows.append({"transporter_id": cid, "vehicle_no": vehicle_no,
                         "vehicle_no_norm": plate, "provenance": "DOCUMENT_EVIDENCED",
                         "assumption_ref": None, "source_ref": doc_variant})

        # --- the generated half: every contract plate evidence did not cover --
        for plate in CONTRACT_PLATES:
            p = norm_plate(plate)
            if p in seen:
                continue
            seen.add(p)
            stats["synthetic"] += 1
            rows.append({"transporter_id": synthetic_pick(p, transporter_ids),
                         "vehicle_no": plate, "vehicle_no_norm": p,
                         "provenance": "SYNTHETIC", "assumption_ref": ASSUMPTION_REF,
                         "source_ref": None})

        for r in sorted(rows, key=lambda x: str(x["vehicle_no_norm"])):
            name = next((n for cid, n in companies if cid == r["transporter_id"]), "?")
            tag = r["provenance"] if r["provenance"] == "DOCUMENT_EVIDENCED" else \
                f"SYNTHETIC ({ASSUMPTION_REF})"
            print(f"  {r['vehicle_no_norm']:<12} -> {name[:44]:<44} {tag}"
                  + (f"  [{r['source_ref']}]" if r["source_ref"] else ""))

        if dry_run:
            print("\n--dry-run: nothing written.")
        else:
            for r in rows:
                await conn.execute(text(
                    "INSERT INTO core.transporter_vehicle "
                    "  (transporter_id, vehicle_no, vehicle_no_norm, provenance, "
                    "   assumption_ref, source_ref) "
                    "VALUES (:transporter_id, :vehicle_no, :vehicle_no_norm, "
                    "        :provenance, :assumption_ref, :source_ref) "
                    "ON CONFLICT (transporter_id, vehicle_no_norm) DO UPDATE SET "
                    "  vehicle_no = EXCLUDED.vehicle_no, "
                    "  provenance = EXCLUDED.provenance, "
                    "  assumption_ref = EXCLUDED.assumption_ref, "
                    "  source_ref = EXCLUDED.source_ref"), r)
                stats["written"] += 1

    await engine.dispose()
    print(f"\nseed complete: evidenced={stats['evidenced']} "
          f"synthetic={stats['synthetic']} unresolved={stats['unresolved']} "
          f"written={stats['written']}  seed={SEED!r}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN", ""),
                    help="SQLAlchemy asyncpg DSN (default: $POSTGRES_DSN)")
    ap.add_argument("--dry-run", action="store_true",
                    help="resolve and report; write nothing")
    args = ap.parse_args()
    if not args.dsn:
        print("POSTGRES_DSN is required", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args.dsn, args.dry_run))
    except SeedError as exc:
        print(f"\nSEED REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
