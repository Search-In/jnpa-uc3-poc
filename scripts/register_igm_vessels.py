#!/usr/bin/env python3
"""Register every vessel an IGM names, keyed by IMO + call sign (GAP-VESSEL-01).

The problem
-----------
16 distinct IMOs appear across the corpus IGMs. `core.vessel` names only 6 of
them, so the other 10 — carrying **7,808 containers** — had no vessel entity at
all. Every vessel-scoped view therefore silently omitted two thirds of the
manifested cargo.

Why this script does NOT set a vessel name
------------------------------------------
Because the corpus does not contain one. Measured 17-Aug-2026:

  * `CHPOI03` IGM XML carries `<IMOCodeofVessel>` and `<VesselCode>` (the call
    sign) and **no name element whatsoever**;
  * each of the 10 call signs (9HA5230, 5LMU7, D5LQ2, 5BZP3, BPHR4, ETAO, D5NN5,
    D5EG7, 9VJK6, VRXQ4) appears in exactly one file — its own IGM;
  * none of the 10 IMOs appears in `core.vessel_call`, `core.pilotage`,
    `core.berthing_record`, `core.pcs_message_log` or
    `core.manual_pilot_assignment`.

So no supplied file can name these ships. Names could be looked up in an
external register (IMO GISIS, Equasis), but a name obtained that way is not
JNPA data and would sit in a deliverable indistinguishable from evidence — the
exact thing the provenance vocabulary exists to prevent. This is reported to
JNPA in 06_DEFECT_REPORT_FOR_JNPA.md instead.

What it does do
---------------
Creates the vessel as a first-class entity under the identity the corpus DOES
supply — IMO and call sign — with `vessel_name` left NULL. That makes the 7,808
containers addressable (by IMO or call sign) without inventing anything, and it
makes "this ship has no name in the corpus" a visible fact rather than an
absence.

Safety
------
* INSERT ... ON CONFLICT (imo_no) DO NOTHING, plus a guarded UPDATE that fills
  `call_sign` only where it is currently NULL. Re-running changes 0 rows.
* Never writes `vessel_name`, and never overwrites an existing call sign — if a
  colleague has named one of these since, this script leaves it alone.
* No DDL, no DELETE. `jnpa_schema_v3` is shared.

Usage
-----
    python scripts/register_igm_vessels.py --dry-run
    python scripts/register_igm_vessels.py
"""
from __future__ import annotations

import argparse
import os
import sys

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "") or os.environ.get("JNPA_RDS_DSN", "")

_FIND = """
    SELECT i.imo_no,
           max(i.vessel_code)                     AS call_sign,
           count(DISTINCT i.igm_no)               AS igms,
           count(DISTINCT lc.container_no)        AS containers,
           v.imo_no IS NOT NULL                   AS already_registered,
           v.vessel_name                          AS existing_name
    FROM core.igm i
    LEFT JOIN core.igm_line_container lc ON lc.igm_no = i.igm_no
    LEFT JOIN core.vessel v ON btrim(v.imo_no) = btrim(i.imo_no)
    WHERE i.imo_no IS NOT NULL AND btrim(i.imo_no) <> ''
    GROUP BY i.imo_no, v.imo_no, v.vessel_name
    ORDER BY containers DESC
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dsn:
        print("No DSN: set POSTGRES_DSN or pass --dsn", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine, text

    engine = create_engine(args.dsn.replace("postgresql://", "postgresql+psycopg://"))
    inserted = call_signs = 0

    with engine.begin() as conn:
        found = conn.execute(text(_FIND)).fetchall()

        print("IGM VESSEL REGISTRATION (GAP-VESSEL-01)"
              + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
        print(f"  {len(found)} distinct IMO(s) across the corpus IGMs\n")
        print(f"  {'IMO':<10} {'CALL SIGN':<10} {'IGMs':>5} {'BOXES':>7}  STATE")

        for imo, call_sign, igms, containers, registered, name in found:
            if registered and name:
                state = f"already named: {name}"
            elif registered:
                state = "registered, name not supplied by JNPA"
            else:
                state = "NEW — will register (name left NULL)"
            print(f"  {imo:<10} {str(call_sign or '-'):<10} {igms:>5} "
                  f"{containers:>7}  {state}")

            if args.dry_run or registered:
                continue

            # `vessel_name` is deliberately absent from the column list: the
            # corpus supplies no name, and a placeholder in a name column would
            # be read as one.
            inserted += conn.execute(text("""
                INSERT INTO core.vessel (imo_no, call_sign, updated_at)
                VALUES (:imo, :cs, now())
                ON CONFLICT (imo_no) DO NOTHING
            """), {"imo": imo, "cs": call_sign}).rowcount

        if not args.dry_run:
            # Fill a missing call sign on rows that already existed, without
            # ever overwriting one that is already set.
            for imo, call_sign, *_ in found:
                if not call_sign:
                    continue
                call_signs += conn.execute(text("""
                    UPDATE core.vessel SET call_sign = :cs, updated_at = now()
                    WHERE btrim(imo_no) = btrim(:imo) AND call_sign IS NULL
                """), {"imo": imo, "cs": call_sign}).rowcount

    if args.dry_run:
        print("\nDRY-RUN complete — nothing written.")
        return 0

    print(f"\n  vessels registered   : {inserted}")
    print(f"  call signs filled in : {call_signs}")

    with engine.connect() as conn:
        total, named, unnamed = conn.execute(text("""
            SELECT count(*),
                   count(*) FILTER (WHERE v.vessel_name IS NOT NULL),
                   count(*) FILTER (WHERE v.vessel_name IS NULL)
            FROM core.vessel v
            WHERE v.imo_no IN (SELECT DISTINCT imo_no FROM core.igm
                               WHERE imo_no IS NOT NULL)
        """)).one()
        reachable = conn.execute(text("""
            SELECT count(*) FROM core.cargo ca
            JOIN core.igm i ON i.igm_no::text = ca.source_igm_no::text
            JOIN core.vessel v ON btrim(v.imo_no) = btrim(i.imo_no)
        """)).scalar_one()
        print(f"\n  IGM vessels now in core.vessel : {total} "
              f"({named} named, {unnamed} unnamed — see the defect report)")
        print(f"  cargo rows reachable via a registered vessel : {reachable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
