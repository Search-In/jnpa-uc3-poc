#!/usr/bin/env python3
"""Name the vessel on the corpus cargo rows, from the manifest that declared them.

THE PROBLEM. `core.cargo` already holds the corpus containers — 11,957 rows
against 11,914 distinct containers in `core.igm_line_container`, with correct
manifest ETAs. But only **24 of them carry a `vessel_name`**. That is why
`GET /api/cargo?vessel_name=XIN HANG ZHOU` returns nothing while the vessel,
its manifest and its 680 boxes are all demonstrably in the database: the boxes
simply never had the ship's name written against them.

THE JOIN. Every hop is real, none is invented:

    core.igm_line_container.container_no  ->  core.cargo.container_number
    core.igm_line_container.igm_no        ->  core.igm.igm_no
    core.igm.imo_no                       ->  core.vessel.imo_no  -> vessel_name

The IGM XMLs carry no vessel NAME (only IMO + call sign), which is the whole
reason this indirection exists; the name comes from the vessel register.

HONEST PARTIAL. Only 6 of the 16 IGM IMOs are present in `core.vessel`, so about
4,300 of the 11,914 containers can be named and the rest stay NULL. That is the
correct outcome — a container whose ship we cannot name should show no ship, not
a guess. The unresolved IMOs are reported so the gap stays visible.

SAFETY. Fills NULLs only (`WHERE vessel_name IS NULL`), so no existing value is
ever overwritten. Idempotent — a second run reports 0. Purely a column fill: no
row is created or removed.

Usage:
    python scripts/backfill_cargo_vessel_from_igm.py --dsn ... --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")

#: One row per container that can be named, with the name to write.
_RESOLVABLE = """
SELECT DISTINCT c.container_no, v.vessel_name
  FROM core.igm_line_container c
  JOIN core.igm    i ON i.igm_no  = c.igm_no
  JOIN core.vessel v ON v.imo_no  = i.imo_no
 WHERE v.vessel_name IS NOT NULL
"""

_UPDATE = f"""
UPDATE core.cargo g
   SET vessel_name = r.vessel_name,
       origin_stream = COALESCE(g.origin_stream, 'CORPUS-IGM')
  FROM ({_RESOLVABLE}) r
 WHERE g.container_number = r.container_no
   AND g.vessel_name IS NULL
"""


async def run(dsn: str, dry_run: bool) -> Dict[str, Any]:
    from sqlalchemy import text
    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    rep: Dict[str, Any] = {}
    async with engine.connect() as conn:
        rep["cargo_total"] = (await conn.execute(text(
            "SELECT count(*) FROM core.cargo"))).scalar()
        rep["named_before"] = (await conn.execute(text(
            "SELECT count(*) FROM core.cargo WHERE vessel_name IS NOT NULL"))).scalar()
        rep["would_fill"] = (await conn.execute(text(
            f"SELECT count(*) FROM core.cargo g "
            f"JOIN ({_RESOLVABLE}) r ON r.container_no = g.container_number "
            f"WHERE g.vessel_name IS NULL"))).scalar()
        rep["per_vessel"] = [dict(x) for x in (await conn.execute(text(
            f"SELECT r.vessel_name, count(*) n FROM core.cargo g "
            f"JOIN ({_RESOLVABLE}) r ON r.container_no = g.container_number "
            f"WHERE g.vessel_name IS NULL GROUP BY 1 ORDER BY 2 DESC"))).mappings()]
        rep["unresolved_imos"] = [dict(x) for x in (await conn.execute(text(
            "SELECT i.igm_no, i.imo_no, i.vessel_code, "
            "       (SELECT count(DISTINCT c.container_no) FROM core.igm_line_container c "
            "         WHERE c.igm_no = i.igm_no) boxes "
            "  FROM core.igm i LEFT JOIN core.vessel v ON v.imo_no = i.imo_no "
            " WHERE v.vessel_name IS NULL ORDER BY boxes DESC"))).mappings()]

    if not dry_run:
        async with engine.begin() as conn:
            await conn.execute(text(_UPDATE))
        async with engine.connect() as conn:
            rep["named_after"] = (await conn.execute(text(
                "SELECT count(*) FROM core.cargo WHERE vessel_name IS NOT NULL"))).scalar()
            rep["rows_after"] = (await conn.execute(text(
                "SELECT count(*) FROM core.cargo"))).scalar()
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = asyncio.run(run(args.dsn, args.dry_run))

    print("\n" + "=" * 70)
    print("CARGO vessel_name BACKFILL FROM IGM"
          + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  core.cargo rows      : {r['cargo_total']}")
    print(f"  named before         : {r['named_before']}")
    print(f"  fillable from IGM    : {r['would_fill']}")
    for x in r["per_vessel"]:
        print(f"      {x['vessel_name']:26} {x['n']}")
    if "named_after" in r:
        print(f"  named after          : {r['named_after']}")
        print(f"  row count after      : {r['rows_after']}  (unchanged = column fill only)")
    if r["unresolved_imos"]:
        print(f"  IMOs with no name in core.vessel: {len(r['unresolved_imos'])}"
              f"  ({sum(x['boxes'] for x in r['unresolved_imos'])} boxes stay unnamed)")
        for x in r["unresolved_imos"][:5]:
            print(f"      igm {x['igm_no']}  imo {x['imo_no']}  code {x['vessel_code']}  boxes {x['boxes']}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
