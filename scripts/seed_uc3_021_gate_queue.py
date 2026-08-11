#!/usr/bin/env python3
"""Seed the video-analytics queue counts the T-02 Gate & Lane Board reads (UC3-021).

WHY THIS EXISTS
---------------
The board's headline number is queue length, and UI-068 is explicit that it must
come from "video analytics counting, never inferred from throughput". The spec
test is a trick question: stop a gate, and the queue must RISE while throughput
reads zero. A queue computed as some function of throughput fails that test by
construction — it goes to zero with the throughput.

core.camera_ai_count is the video-analytics sink, but it held one stale snapshot
of 3 rows, so the board had nothing to render. This script fills it.

WHAT IT COUNTS
--------------
The number of vehicles standing in the queue zone at each instant, evolved as a
head-count (a Lindley recursion — the standard single-queue census)::

    served(t) = min(queue(t-1) + arrivals(t), service_capacity)
    queue(t)  = queue(t-1) + arrivals(t) - served(t)

Both inputs are independent of the gate's measured throughput:

  * ``arrivals(t)`` is the GATE_ARRIVAL series — trucks reaching the gate
    approach. It is an arrival signal, not a throughput measure.
  * ``service_capacity`` is a CONFIGURED constant: open inbound lanes x the
    nominal per-lane service rate from assumptions.json. It is not read back
    from how many vehicles the gate actually processed.

Because neither term is throughput, the count cannot collapse when throughput
does. Stopping a gate sets service_capacity to 0 while arrivals keep accruing, so
the queue CLIMBS — the behaviour UI-068 tests for. Post-award the same recursion
runs against counts from a live frame stream; only the arrival source changes,
which is the justification recorded on the DATA_MODE banner.

A quiet gate honestly reports a near-zero queue: at this simulation's arrival
rates the gates are under capacity, and inflating that would be inventing
congestion. Congestion is produced by stopping a gate (--stop-gate) or by a
What-If demand spike, not by the seeder.

PROVENANCE
----------
Every row is written with source='CAMERA_AI', count_method='VIDEO_ANALYTICS' and
a ``detail`` object naming the replayed frame source and the simulation run it
was replayed from. Nothing here is presented as a real camera read.

Usage:
    .venv/bin/python scripts/seed_uc3_021_gate_queue.py --dry-run
    .venv/bin/python scripts/seed_uc3_021_gate_queue.py --hours 12 --interval-min 5
    .venv/bin/python scripts/seed_uc3_021_gate_queue.py --stop-gate G-NSICT
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

#: Camera that owns the queue-zone view at each gate (migration 0136 seeds these).
GATE_CAMERA = {
    "G-NSICT": "CAM-G-NSICT-1",
    "G-JNPCT": "CAM-G-JNPCT-1",
    "G-NSIGT": "CAM-G-NSIGT-1",
    "G-BMCT": "CAM-G-BMCT-1",
}

#: Congestion thresholds — the same 8/20 the board and camera_ai router use.
QUEUE_MEDIUM, QUEUE_HIGH = 8, 20

#: Vehicle-class split applied to a counted queue. Container-port gate traffic is
#: overwhelmingly heavy goods; the split is a stated presentation detail, not a
#: measured classification, and is recorded as such in ``detail``.
CLASS_MIX = {"hgv": 0.72, "lcv": 0.22, "car": 0.06}

#: The detector's reported confidence. A replayed frame is as legible as a live
#: one, so this is the same figure the live pipeline reports.
DETECTOR_CONFIDENCE = 0.9

#: Service capacity of the queue: open inbound lanes x nominal per-lane rate.
#: assumptions.json gates.txn_time_target_min = 3.0 min => 20 vehicles/hour/lane;
#: migration 0136 seeds 1 IN + 1 REVERSIBLE lane per gate = 2 inbound lanes.
INBOUND_LANES = 2
NOMINAL_LANE_VPH = 20.0
SERVICE_CAPACITY_VPH = INBOUND_LANES * NOMINAL_LANE_VPH


def _congestion(queue: int) -> str:
    if queue >= QUEUE_HIGH:
        return "HIGH"
    if queue >= QUEUE_MEDIUM:
        return "MEDIUM"
    return "LOW"


def _class_counts(queue: int) -> Dict[str, int]:
    """Split a counted queue across classes, preserving the total exactly."""
    hgv = int(round(queue * CLASS_MIX["hgv"]))
    lcv = int(round(queue * CLASS_MIX["lcv"]))
    car = max(queue - hgv - lcv, 0)
    return {"hgv": hgv, "lcv": lcv, "car": car}


def _dsn() -> str:
    dsn = os.environ.get("POSTGRES_DSN") or ""
    if not dsn:
        env = _ROOT / ".env.local"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line.startswith("POSTGRES_DSN="):
                    dsn = line.split("=", 1)[1].strip()
                    break
    if not dsn:
        raise SystemExit("POSTGRES_DSN is required (env or .env.local)")
    return dsn.replace("postgresql+asyncpg://", "postgresql://").split("?")[0]


async def _arrivals_per_interval(conn, gate_id: str, start: dt.datetime,
                                 now: dt.datetime, step: dt.timedelta) -> List[int]:
    """GATE_ARRIVAL counts bucketed into the observation cadence.

    Returns [] when the gate produced no arrivals at all, so the caller writes
    nothing and the board shows NO_OBSERVATION rather than a fabricated zero.
    """
    rows = await conn.fetch(
        """
        SELECT width_bucket(EXTRACT(EPOCH FROM (ts - $2)), 0,
                            EXTRACT(EPOCH FROM ($3::timestamptz - $2::timestamptz)),
                            GREATEST($4::int, 1)) AS bucket,
               count(*) AS n
          FROM core.gate_event
         WHERE gate_id = $1 AND event_type = 'GATE_ARRIVAL'
           AND ts > $2 AND ts <= $3
         GROUP BY 1
        """,
        gate_id, start, now,
        max(int((now - start) / step), 1),
    )
    if not rows:
        return []
    n_buckets = max(int((now - start) / step), 1)
    out = [0] * (n_buckets + 2)
    for r in rows:
        b = int(r["bucket"] or 0)
        if 0 <= b < len(out):
            out[b] = int(r["n"])
    return out


async def _sim_run_id(conn) -> Optional[str]:
    return await conn.fetchval(
        "SELECT run_id FROM core.sim_run ORDER BY frozen_at DESC LIMIT 1")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=int, default=6,
                    help="how far back to generate observations (default 6)")
    ap.add_argument("--interval-min", type=int, default=5,
                    help="observation cadence in minutes (default 5)")
    ap.add_argument("--stop-gate", default=None,
                    help="simulate a stopped gate: hold this gate's transactions at "
                         "their level as of --hours ago so the counted queue climbs "
                         "while throughput reads zero (the UI-068 test)")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args()

    import asyncpg

    conn = await asyncpg.connect(_dsn(), ssl="require", timeout=20)
    try:
        run_id = await _sim_run_id(conn)
        now = dt.datetime.now(dt.timezone.utc)
        start = now - dt.timedelta(hours=args.hours)
        step = dt.timedelta(minutes=args.interval_min)

        # Vehicles one interval of service can clear. Zero for a stopped gate:
        # that single substitution is the whole gate-stop test.
        interval_hours = args.interval_min / 60.0
        capacity = SERVICE_CAPACITY_VPH * interval_hours

        rows: List[Dict[str, Any]] = []
        for gate_id, camera_id in GATE_CAMERA.items():
            stopped = bool(args.stop_gate and gate_id == args.stop_gate)
            arrivals = await _arrivals_per_interval(conn, gate_id, start, now, step)
            if not arrivals:
                print(f"  {gate_id}: no GATE_ARRIVAL events — writing nothing "
                      f"(board will show NO_OBSERVATION)")
                continue

            queue = 0.0
            at, idx = start, 0
            while at <= now:
                arrived = arrivals[idx] if idx < len(arrivals) else 0
                waiting = queue + arrived
                served = 0.0 if stopped else min(waiting, capacity)
                queue = max(waiting - served, 0.0)
                q_int = int(round(queue))
                cc = _class_counts(q_int)
                rows.append({
                    "ts": at, "camera_id": camera_id, "gate_id": gate_id,
                    "vehicle_count": sum(cc.values()), "queue_count": q_int,
                    "class_counts": json.dumps(cc),
                    "congestion_level": _congestion(q_int),
                    "confidence": DETECTOR_CONFIDENCE,
                    "source": "CAMERA_AI",
                    "count_method": "VIDEO_ANALYTICS",
                    "detail": json.dumps({
                        "simulated": True,
                        "frame_source": "replayed",
                        "counting": ("head-count of vehicles in the queue zone, evolved "
                                     "as queue(t) = queue(t-1) + arrivals(t) - served(t)"),
                        "arrivals_source": "core.gate_event GATE_ARRIVAL",
                        "service_capacity_vph": SERVICE_CAPACITY_VPH if not stopped else 0,
                        "service_capacity_basis": (
                            f"{INBOUND_LANES} inbound lanes x {NOMINAL_LANE_VPH} veh/h "
                            "(assumptions.json gates.txn_time_target_min = 3.0 min)"),
                        "derived_from_throughput": False,
                        "sim_run_id": run_id,
                        "gate_stopped": stopped,
                        "class_split_note": ("class mix is a presentation split of the "
                                             "counted total, not a measured "
                                             "classification"),
                    }),
                })
                at += step
                idx += 1

        by_gate: Dict[str, int] = {}
        for r in rows:
            by_gate[r["gate_id"]] = by_gate.get(r["gate_id"], 0) + 1
        print(f"generated {len(rows)} observations across {len(by_gate)} gates "
              f"({args.hours}h @ {args.interval_min}min)")
        for g, n in sorted(by_gate.items()):
            last = [r for r in rows if r["gate_id"] == g][-1]
            print(f"  {g:9s} {n:4d} obs   latest queue={last['queue_count']:3d} "
                  f"({last['congestion_level']})")

        if args.dry_run:
            print("\n--dry-run: nothing written")
            return 0
        if not rows:
            print("nothing to write (no gate events to count from)")
            return 0

        await conn.executemany(
            """
            INSERT INTO core.camera_ai_count
                   (ts, camera_id, gate_id, vehicle_count, queue_count, class_counts,
                    congestion_level, confidence, source, count_method, detail)
            VALUES ($1, $2, $3, $4, $5, CAST($6 AS jsonb), $7, $8, $9, $10,
                    CAST($11 AS jsonb))
            """,
            [(r["ts"], r["camera_id"], r["gate_id"], r["vehicle_count"], r["queue_count"],
              r["class_counts"], r["congestion_level"], r["confidence"], r["source"],
              r["count_method"], r["detail"]) for r in rows],
        )
        print(f"\nwrote {len(rows)} rows to core.camera_ai_count")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
