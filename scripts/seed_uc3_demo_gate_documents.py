#!/usr/bin/env python3
"""Give the UC-III demo a pool of ASSIGNABLE containers (BUG-3).

WHAT THIS FIXES
---------------
``ContainerJobService.validate_assignment`` refuses to dispatch a truck against a
box with no paperwork: at least one FORM13 / PIN / EIR must reference the
container (``repository.document_counts``). That rule is correct and is NOT
changed here.

The problem was coverage, not logic. On the PoC database:

    core.cargo                                   11,944 containers
    core.gate_capture WHERE capture_type=FORM13     203 distinct containers
    ...of which present in core.cargo                 4   <-- assignable total

The two corpora were imported from different sources and never reconciled, so
the Container Operations console had exactly four containers it could ever
assign — and all four had already been consumed by the existing demo jobs. This
script closes that gap by issuing a Form-13 for containers that ARE in the cargo
registry, which is the same document type the real importer writes.

WHAT IT WRITES
--------------
One ``core.gate_capture`` row per selected container:

    capture_type  FORM13
    container_no  <the cargo container>
    payload       {form13_no, container_no, cargo_desc, gross_wt_kg, seeded_by}

``payload.seeded_by = 'seed_uc3_demo_gate_documents'`` marks every row this
script owns, so demo paperwork is always distinguishable from the imported
corpus and can be removed with a single DELETE (``--purge``).

SAFETY
------
  * ADDITIVE ONLY. Never updates or deletes imported rows, never touches
    core.cargo, core.container_job_assignment or any master.
  * IDEMPOTENT. Re-running selects only containers that still lack a document,
    and the insert is ON CONFLICT DO NOTHING against the natural key
    (container_no, capture_type, captured_at).
  * Containers are filtered through the project's own ISO-6346 validator, so a
    seeded box can never fail the check-digit gate the assignment applies.
  * --dry-run prints the plan and writes nothing.

USAGE
-----
    python scripts/seed_uc3_demo_gate_documents.py --dsn "$POSTGRES_DSN" --count 24
    python scripts/seed_uc3_demo_gate_documents.py --dsn ... --dry-run
    python scripts/seed_uc3_demo_gate_documents.py --dsn ... --purge
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

from sqlalchemy import text  # noqa: E402

from jnpa_shared.db import get_engine  # noqa: E402
from jnpa_shared.iso6346 import is_valid_container_no  # noqa: E402

SEEDED_BY = "seed_uc3_demo_gate_documents"

# Plausible import cargo descriptions — the Form-13 carries one, and a demo
# reads better than 24 identical "DEMO CARGO" rows.
CARGO_DESCS = (
    "ELECTRONIC COMPONENTS", "COTTON YARN", "CERAMIC TILES", "AUTO SPARE PARTS",
    "PHARMACEUTICALS", "FURNITURE FITTINGS", "STAINLESS STEEL COIL",
    "POLYPROPYLENE GRANULES", "LEATHER FOOTWEAR", "MACHINE TOOLS",
    "PAPER REELS", "GLASSWARE",
)

# Containers already carrying paperwork, by the same probe the assignment uses.
_HAS_DOC = """
    SELECT 1 FROM core.eir e WHERE e.container_number = c.container_number
    UNION ALL
    SELECT 1 FROM core.pin_ticket p WHERE p.container_number = c.container_number
    UNION ALL
    SELECT 1 FROM core.gate_capture g
     WHERE g.capture_type = 'FORM13' AND g.container_no = c.container_number
"""

SELECT_CANDIDATES = f"""
SELECT c.container_number
  FROM core.cargo c
 WHERE c.container_number ~ '^[A-Z]{{4}}[0-9]{{7}}$'
   AND NOT EXISTS ({_HAS_DOC})
 ORDER BY c.container_number
 LIMIT :scan
"""

COUNT_ASSIGNABLE = f"""
SELECT count(*) FROM core.cargo c WHERE EXISTS ({_HAS_DOC})
"""

INSERT_FORM13 = """
INSERT INTO core.gate_capture
       (capture_type, container_no, source_mode, status, captured_at, payload)
VALUES ('FORM13', :cn, 'sim', 'REGISTERED', now(),
        CAST(:payload AS jsonb))
ON CONFLICT (container_no, capture_type, captured_at) DO NOTHING
"""

PURGE = """
DELETE FROM core.gate_capture
 WHERE capture_type = 'FORM13' AND payload->>'seeded_by' = :by
"""


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN"),
                    help="SQLAlchemy DSN (defaults to $POSTGRES_DSN)")
    ap.add_argument("--count", type=int, default=24,
                    help="how many containers to make assignable (default 24)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--purge", action="store_true",
                    help="remove ONLY the Form-13 rows this script created")
    args = ap.parse_args()

    if not args.dsn:
        print("ERROR: --dsn or $POSTGRES_DSN is required", file=sys.stderr)
        return 2

    engine = get_engine(args.dsn)

    if args.purge:
        async with engine.begin() as conn:
            n = (await conn.execute(text(PURGE), {"by": SEEDED_BY})).rowcount
        print(f"purged {n} seeded Form-13 row(s)")
        return 0

    async with engine.connect() as conn:
        before = int((await conn.execute(text(COUNT_ASSIGNABLE))).scalar() or 0)
        # Over-scan: the ISO-6346 check digit rejects some of the corpus, so ask
        # for more rows than needed and filter in Python with the real validator.
        rows = (await conn.execute(text(SELECT_CANDIDATES),
                                   {"scan": max(args.count * 20, 400)})).scalars().all()

    picked = [cn for cn in rows if is_valid_container_no(cn)][:args.count]
    if not picked:
        print(f"nothing to do — {before} container(s) already assignable, "
              "no ISO-6346-valid candidate without paperwork")
        return 0

    print(f"assignable before : {before}")
    print(f"candidates scanned: {len(rows)} (ISO-valid, undocumented)")
    print(f"selected          : {len(picked)}")
    for i, cn in enumerate(picked):
        print(f"  {cn}  {CARGO_DESCS[i % len(CARGO_DESCS)]}")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    import json

    written = 0
    async with engine.begin() as conn:
        for i, cn in enumerate(picked):
            payload = {
                "form13_no": f"F13DEMO{9000000 + i}",
                "container_no": cn,
                "cargo_desc": CARGO_DESCS[i % len(CARGO_DESCS)],
                "gross_wt_kg": 18000 + (i * 137) % 9000,
                "seeded_by": SEEDED_BY,
            }
            written += (await conn.execute(
                text(INSERT_FORM13), {"cn": cn, "payload": json.dumps(payload)})).rowcount

    async with engine.connect() as conn:
        after = int((await conn.execute(text(COUNT_ASSIGNABLE))).scalar() or 0)

    print(f"\nwrote {written} Form-13 row(s)")
    print(f"assignable after  : {after}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
