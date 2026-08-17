#!/usr/bin/env python3
"""Complete the OPERATIONAL flow with clearly-labelled synthetic events.

WHY. The corpus records manifests at scale (11,914 containers) and gate events
almost not at all: measured on RDS, **42 of 11,957 containers reach a truck by any
route**, because JNPA's manifest set and its gate-document set share no containers.
That is a real, reported defect (`06_DEFECT_REPORT_FOR_JNPA.md` A1) — but it also
means the container→truck→gate flow cannot be *demonstrated*, because there is
almost nothing to demonstrate it with.

This seeds that missing operational middle for a bounded cohort of REAL containers
taken from a REAL manifest, using REAL plates and REAL transporters, so the
functional flow can be exercised end to end.

WHAT IT DOES NOT DO. It does not touch the customs or manifest evidence — no
synthetic IGM lines, OOC, LEO, shipping bills or Form 13. Those are the documents
the corpus defect report reasons about, and fabricating them would corrupt the
very analysis that makes this submission credible. Only the operational hops the
corpus omits are generated:

    job assignment -> gate-in -> yard move -> gate-out -> lifecycle

core.codeco_movement is deliberately NOT written: its data_origin CHECK allows
only 'API'/'MANUAL', so a synthetic row there could not be labelled as one, and
that table holds the five genuine corpus gate-outs.

EVERY ROW IS MARKED, in the column each table already uses for provenance:

    core.gate_event.source              = 'SYNTHETIC:flow-v1'
    core.cargo_movement_event.actor     = 'SYNTHETIC:flow-v1'
    core.container_job_assignment.assigned_by = 'SYNTHETIC:flow-v1'
    core.cargo.origin_stream            = 'SYNTHETIC-FLOW'

`GET /api/thread/container/{no}` surfaces `data_origin` on every hop, so a
synthetic step is visibly synthetic wherever it is rendered.

REVERSIBLE. `--teardown` removes exactly the rows carrying the marker and nothing
else. This is the one place a DELETE is correct: removing fixtures this script
created, scoped by a marker no real row carries. Always dry-run it first — the
count it reports is the count it will delete.

Usage:
    python scripts/seed_synthetic_flow.py --dsn ... --dry-run
    python scripts/seed_synthetic_flow.py --dsn ... --vessel "XIN HANG ZHOU" --count 120
    python scripts/seed_synthetic_flow.py --dsn ... --teardown --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")

#: The one marker. Every synthetic row carries it, in whichever column that table
#: uses for provenance, and teardown keys on exactly this string.
MARKER = "SYNTHETIC:flow-v1"
ORIGIN_STREAM = "SYNTHETIC-FLOW"
ASSUMPTION = "A-SIM-01"
IST = timezone(timedelta(hours=5, minutes=30))

#: Deterministic, so a re-run reproduces the same timings — no Date.now()/random.
_BASE = datetime(2026, 6, 8, 6, 0, tzinfo=IST)

#: Gate-pass prefix marking synthetic CODECO rows (see _TEARDOWN).
_GP_PREFIX = "SIMGP"


async def _cohort(conn, vessel: str, count: int) -> List[Dict[str, Any]]:
    """Real containers from a real manifest, that do NOT already have a gate move.

    Excluding boxes that already have one keeps the genuine chain (DPWU9011100 →
    MH46H6948) untouched and unambiguous.
    """
    from sqlalchemy import text
    rows = (await conn.execute(text("""
        SELECT g.container_number
          FROM core.cargo g
         WHERE upper(regexp_replace(btrim(g.vessel_name), '\\s+', ' ', 'g'))
             = upper(regexp_replace(btrim(:v),  '\\s+', ' ', 'g'))
           AND NOT EXISTS (SELECT 1 FROM core.codeco_movement m
                            WHERE m.container_no = g.container_number)
           AND NOT EXISTS (SELECT 1 FROM core.container_job_assignment j
                            WHERE j.container_number = g.container_number)
         ORDER BY g.container_number
         LIMIT :n"""), {"v": vessel, "n": count})).mappings().all()
    return [dict(r) for r in rows]


async def _fleet(conn) -> List[Dict[str, Any]]:
    """Real plates with a real transporter, so the synthetic flow points at the
    actual fleet rather than invented vehicles."""
    from sqlalchemy import text
    rows = (await conn.execute(text("""
        SELECT tv.vehicle_no_norm AS plate, tv.transporter_id, tv.driver_id,
               t.company_name
          FROM core.transporter_vehicle tv
          LEFT JOIN core.transporter t ON t.id = tv.transporter_id
         ORDER BY tv.vehicle_no_norm"""))).mappings().all()
    return [dict(r) for r in rows]


async def plan(conn, vessel: str, count: int) -> Dict[str, Any]:
    cohort = await _cohort(conn, vessel, count)
    fleet = await _fleet(conn)
    return {"vessel": vessel, "containers": [c["container_number"] for c in cohort],
            "fleet": [f["plate"] for f in fleet], "count": len(cohort)}


async def seed(conn, vessel: str, count: int) -> Dict[str, int]:
    from sqlalchemy import text
    cohort = await _cohort(conn, vessel, count)
    fleet = await _fleet(conn)
    if not cohort or not fleet:
        return {"containers": 0, "jobs": 0, "gate_events": 0, "yard_moves": 0}

    jobs = gates = yards = 0
    for i, row in enumerate(cohort):
        cn = row["container_number"]
        f = fleet[i % len(fleet)]
        # Deterministic spread: ~6 boxes an hour across the demo window.
        t_in = _BASE + timedelta(minutes=10 * i)
        t_yard = t_in + timedelta(minutes=35)
        t_out = t_in + timedelta(minutes=95)
        trip = f"SIMTRIP-{i:05d}"

        await conn.execute(text("""
            INSERT INTO core.container_job_assignment
              (container_number, transporter_id, vehicle_id, vehicle_no, driver_id,
               move_type, document_type, terminal, gate, status, assigned_by,
               assigned_at, completed_at, notes)
            VALUES (:cn, :tid, :veh, :veh, :did, 'IMPORT_PICK', 'GATEPASS', 'NSICT',
                    'GATE-1', 'COMPLETED', :marker, :t_in, :t_out, :note)
        """), {"cn": cn, "tid": f["transporter_id"], "veh": f["plate"],
               "did": f["driver_id"], "marker": MARKER, "t_in": t_in, "t_out": t_out,
               "note": f"{MARKER} · assumption {ASSUMPTION} · operational hop absent from corpus"})
        jobs += 1

        for ev, ts in (("GATE_IN", t_in), ("GATE_OUT", t_out)):
            await conn.execute(text("""
                INSERT INTO core.gate_event
                  (ts, device_id, plate, gate_id, trip_id, event_type, container_number,
                   document_type, source)
                VALUES (:ts, :dev, :plate, 'GATE-1', :trip, :ev, :cn, 'GATEPASS', :marker)
            """), {"ts": ts, "dev": f"SIM-{f['plate']}", "plate": f["plate"],
                   "trip": trip, "ev": ev, "cn": cn, "marker": MARKER})
            gates += 1

        await conn.execute(text("""
            INSERT INTO core.cargo_movement_event
              (container_number, movement_type, vehicle_no, yard_location, terminal,
               occurred_at, actor, detail)
            VALUES (:cn, 'YARD_PICKUP', :veh, :yard, 'NSICT', :ts, :marker,
                    CAST(:detail AS jsonb))
        """), {"cn": cn, "veh": f["plate"], "yard": f"S{(i % 9) + 1}B{(i % 20) + 1:02d}",
               "ts": t_yard, "marker": MARKER,
               # `detail` is jsonb, not text — a bare marker string is not valid JSON.
               "detail": json.dumps({"synthetic": True, "marker": MARKER,
                                     "assumption": ASSUMPTION,
                                     "reason": "operational hop absent from corpus"})})
        yards += 1

        # NO synthetic CODECO row, deliberately. core.codeco_movement has
        # CHECK ck_codeco_movement_data_origin = ANY('API','MANUAL') — its
        # vocabulary cannot express "synthetic", and altering a constraint on a
        # database five engineers share is not this script's business. Writing
        # these as 'MANUAL' would put 120 fabricated rows in the one table that
        # holds the five GENUINE corpus gate-outs (incl. DPWU9011100 ->
        # MH46H6948), making the real evidence indistinguishable from the fixture.
        # The gate-out is represented by the GATE_OUT gate_event above, which
        # carries an honest source marker.

        await conn.execute(text("""
            UPDATE core.cargo
               SET lifecycle_status = 'RELEASED', is_released = true,
                   vehicle_number = :veh, yard_block = :yard,
                   origin_stream = :os
             WHERE container_number = :cn AND lifecycle_status = 'CREATED'
        """), {"cn": cn, "veh": f["plate"], "yard": f"S{(i % 9) + 1}", "os": ORIGIN_STREAM})

    return {"containers": len(cohort), "jobs": jobs, "gate_events": gates,
            "yard_moves": yards}


# core.codeco_movement.source_file is an INTEGER file id and cannot carry the text
# marker; its synthetic rows are identified by data_origin='SIM' AND the gate-pass
# prefix above — a pair no corpus row has (real CODECO rows are 'MANUAL' with
# numeric gate passes).
_TEARDOWN = [
    ("core.gate_event", "source = :m"),
    ("core.cargo_movement_event", "actor = :m"),
    ("core.container_job_assignment", "assigned_by = :m"),
]


async def teardown(conn, dry_run: bool) -> Dict[str, int]:
    from sqlalchemy import text
    counts: Dict[str, int] = {}
    for table, where in _TEARDOWN:
        n = (await conn.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"),
                                {"m": MARKER})).scalar() or 0
        counts[table] = int(n)
        if not dry_run and n:
            await conn.execute(text(f"DELETE FROM {table} WHERE {where}"), {"m": MARKER})
    n = (await conn.execute(text("SELECT count(*) FROM core.cargo WHERE origin_stream = :o"),
                            {"o": ORIGIN_STREAM})).scalar() or 0
    counts["core.cargo (lifecycle reset)"] = int(n)
    if not dry_run and n:
        # Restore the rows to the state the corpus left them in.
        await conn.execute(text("""
            UPDATE core.cargo
               SET lifecycle_status = 'CREATED', is_released = false,
                   vehicle_number = NULL, yard_block = NULL,
                   origin_stream = 'CORPUS-IGM'
             WHERE origin_stream = :o"""), {"o": ORIGIN_STREAM})
    return counts


async def run(dsn: str, vessel: str, count: int, dry_run: bool, do_teardown: bool):
    from jnpa_shared.db import get_engine
    engine = get_engine(dsn)
    if do_teardown:
        async with engine.begin() as conn:
            return {"mode": "teardown", "counts": await teardown(conn, dry_run)}
    async with engine.connect() as conn:
        preview = await plan(conn, vessel, count)
    if dry_run:
        return {"mode": "seed", "preview": preview}
    async with engine.begin() as conn:
        return {"mode": "seed", "preview": preview, "written": await seed(conn, vessel, count)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=DEFAULT_DSN, required=not DEFAULT_DSN)
    ap.add_argument("--vessel", default="XIN HANG ZHOU")
    ap.add_argument("--count", type=int, default=120)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--teardown", action="store_true",
                    help=f"remove every row marked {MARKER} and reset the cargo rows")
    a = ap.parse_args()
    r = asyncio.run(run(a.dsn, a.vessel, a.count, a.dry_run, a.teardown))

    print("\n" + "=" * 70)
    print(f"SYNTHETIC OPERATIONAL FLOW · {r['mode'].upper()}"
          + ("  [DRY-RUN — no DB writes]" if a.dry_run else ""))
    print("=" * 70)
    print(f"  marker : {MARKER}   (assumption {ASSUMPTION})")
    if r["mode"] == "teardown":
        for k, v in r["counts"].items():
            print(f"    {k:38} {v}")
    else:
        p = r["preview"]
        print(f"  vessel          : {p['vessel']}")
        print(f"  eligible boxes  : {p['count']}  (real manifest rows with no existing gate move)")
        print(f"  fleet used      : {len(p['fleet'])} real plates -> {', '.join(p['fleet'][:5])}…")
        if "written" in r:
            print("-" * 70)
            for k, v in r["written"].items():
                print(f"    {k:20} {v}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
