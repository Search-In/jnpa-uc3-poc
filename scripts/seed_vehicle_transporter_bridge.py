#!/usr/bin/env python3
"""Complete the vehicle -> transporter -> driver bridge for the real corpus plates.

BACKGROUND. `11-Transport Data` carries 2,191 transporters and 31,846 drivers but
**no vehicle-registration column at all**, so a truck plate read off a gate slip
resolves to nobody. That is corpus gap G6, and it blocks the UC-III blacklist,
fleet view and any per-transporter attribution.

Most of the bridge already exists in `core.transporter_vehicle`, and it already
distinguishes evidence from assumption:

    provenance = 'DOCUMENT_EVIDENCED'  the slip names the company; source_ref
                                       records which document
    provenance = 'SYNTHETIC'           no evidence anywhere; assumption_ref='A-G6'

This script closes the two remaining holes, and nothing else:

  1. **A real link that was left NULL.** EIRs `5599372` and `5614330` both print
     driver `BABALU KUMAR`, licence `UP6420140008203`, on plate `MH43BX1488`. That
     licence DOES resolve in the PDP master (`core.driver`, normalised) — an
     earlier analysis of the raw files concluded it did not, because it compared
     un-normalised strings. So this link is DOCUMENT_EVIDENCED, not assumed.

  2. **One unmapped plate.** `MH04QI2192` appears on a PIN ticket and in no
     mapping. No transporter is printed for it anywhere in the corpus, so it is
     added as SYNTHETIC / A-G6 — visibly an assumption, never dressed as fact.

Idempotent: the driver link only fills a NULL, and the insert is guarded by NOT
EXISTS. Re-running reports 0 changes. Purely additive apart from that one NULL
fill; no row is deleted and no evidenced value is overwritten.

Usage:
    python scripts/seed_vehicle_transporter_bridge.py --dsn ... --dry-run
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

#: The one licence the corpus evidences against a plate, and its document.
_EVIDENCED_DRIVER = {
    "plate": "MH43BX1488",
    "licence": "UP6420140008203",
    "docs": "eir3_gateway_maersk+eir4_gateway_one",
}
#: Plates seen in the corpus with no transporter printed anywhere.
_UNEVIDENCED_PLATES = ("MH04QI2192",)
_ASSUMPTION = "A-G6"


async def run(dsn: str, dry_run: bool) -> Dict[str, Any]:
    from sqlalchemy import text
    from jnpa_shared.db import get_engine

    engine = get_engine(dsn)
    report: Dict[str, Any] = {"driver_links": 0, "synthetic_added": 0, "notes": []}

    async with engine.connect() as conn:
        # --- 1. the evidenced driver link -------------------------------------
        drv = (await conn.execute(text(
            "SELECT driver_id, driver_name FROM core.driver "
            "WHERE replace(upper(licence_no_norm),' ','') = replace(upper(:lic),' ','')"),
            {"lic": _EVIDENCED_DRIVER["licence"]})).mappings().first()
        if not drv:
            report["notes"].append(
                f"licence {_EVIDENCED_DRIVER['licence']} not in core.driver — link skipped")
        else:
            pending = (await conn.execute(text(
                "SELECT count(*) FROM core.transporter_vehicle "
                "WHERE vehicle_no_norm = :p AND driver_id IS NULL"),
                {"p": _EVIDENCED_DRIVER["plate"]})).scalar() or 0
            report["driver_links"] = int(pending)
            report["notes"].append(
                f"{_EVIDENCED_DRIVER['plate']} -> driver_id {drv['driver_id']} "
                f"({drv['driver_name']}) [DOCUMENT_EVIDENCED]")

        # --- 2. the unevidenced plates ----------------------------------------
        to_add = []
        for plate in _UNEVIDENCED_PLATES:
            exists = (await conn.execute(text(
                "SELECT count(*) FROM core.transporter_vehicle WHERE vehicle_no_norm = :p"),
                {"p": plate})).scalar() or 0
            if not exists:
                company = (await conn.execute(text(
                    "SELECT company FROM core.pin_ticket WHERE upper(truck_no) = :p "
                    "AND company IS NOT NULL LIMIT 1"), {"p": plate})).scalar()
                tid = None
                if company:
                    tid = (await conn.execute(text(
                        "SELECT id FROM core.transporter "
                        "WHERE company_name ILIKE '%' || :c || '%' LIMIT 1"),
                        {"c": company.split()[0]})).scalar()
                to_add.append((plate, tid, company))
        report["synthetic_added"] = len(to_add)
        for plate, tid, company in to_add:
            report["notes"].append(
                f"{plate} -> transporter {tid} (from PIN company {company!r}) "
                f"[SYNTHETIC / {_ASSUMPTION}]" if tid else
                f"{plate} -> no transporter resolvable [SYNTHETIC / {_ASSUMPTION}]")

    if dry_run:
        return report

    async with engine.begin() as conn:
        if drv:
            await conn.execute(text(
                "UPDATE core.transporter_vehicle "
                "   SET driver_id = :did, "
                # Explicit cast: Postgres cannot infer a bare parameter's type
                # inside concat_ws() and raises IndeterminateDatatypeError.
                "       source_ref = concat_ws('+', nullif(source_ref,''), CAST(:docs AS text)) "
                " WHERE vehicle_no_norm = :p AND driver_id IS NULL"),
                # core.transporter_vehicle.driver_id / transporter_id are TEXT
                # columns, not integers — pass the id as a string.
                {"did": str(drv["driver_id"]), "p": _EVIDENCED_DRIVER["plate"],
                 "docs": _EVIDENCED_DRIVER["docs"]})
        for plate, tid, _company in to_add:
            await conn.execute(text(
                "INSERT INTO core.transporter_vehicle "
                "  (transporter_id, vehicle_no, vehicle_no_norm, provenance, assumption_ref) "
                "SELECT :tid, :p, :p, 'SYNTHETIC', :a "
                " WHERE NOT EXISTS (SELECT 1 FROM core.transporter_vehicle "
                "                    WHERE vehicle_no_norm = :p)"),
                # Mixed column types in this table: transporter_id is an INTEGER
                # while driver_id above is TEXT. Pass each as its own type.
                {"tid": None if tid is None else int(tid), "p": plate, "a": _ASSUMPTION})
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    rep = asyncio.run(run(args.dsn, args.dry_run))

    print("\n" + "=" * 70)
    print("VEHICLE -> TRANSPORTER -> DRIVER BRIDGE"
          + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 70)
    print(f"  driver links to fill : {rep['driver_links']}")
    print(f"  synthetic mappings   : {rep['synthetic_added']}")
    for n in rep["notes"]:
        print(f"    - {n}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
