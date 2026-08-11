#!/usr/bin/env python3
"""Deterministic 20k-truck NH-348 corridor simulation (UC3-005).

There is no real per-truck GPS for the demo window, so corridor traffic at 20k
scale must be generated. This script generates it in a way that is reproducible,
auditable and impossible to mistake for measured data:

  * DETERMINISTIC — every per-truck decision derives from
    sha256(SEED || truck index). No RNG, no wall-clock, no dict ordering. The
    same seed produces byte-identical trucks on any machine, so a rehearsal can
    be replayed exactly.
  * FROZEN — SEED and SEED_VERSION are module constants, and a SHA-256 over the
    whole config is stored on the run. If anyone reseeds after rehearsal the
    hash changes and the change is visible; the hash is the tamper-evidence.
  * IDEMPOTENT — trucks upsert on (run_id, truck_uid), so a re-run updates in
    place and never creates a 40,001st truck.
  * LABELLED — sim_truck.simulated and .provenance are pinned to true/'SIMULATED'
    by CHECK constraints in migration 0135. Nothing generated here can be
    relabelled real, and nothing real can be written into these tables.
  * ISOLATED — writes ONLY to core.sim_run and core.sim_truck. No measured store
    (gate_document, container_event, transporter, ...) is touched, so UC3-001,
    UC3-002 and UC3-003 data cannot be contaminated.

Calibration: the IN/OUT split is scaled from the anchor day's PUBLISHED gate
moves (20-07-2026: 6,462 IN / 11,490 OUT / 17,952 total TEU). The real ratio is
reproduced in the generated population rather than invented, and the anchor
figures are stored on the run so the calibration can be re-checked later.

Usage:
    UC3_TARGET_DB=jnpa_schema_v3 POSTGRES_DSN=... \\
        ./.venv/bin/python scripts/seed_uc3_005_corridor_simulation.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# ---- FROZEN CONFIG. Changing any value here changes config_sha256. ----------
RUN_ID = "uc3-005-nh348-20k"
CORRIDOR = "NH-348"
SEED = "UC3-005:NH-348:20k:v1"
SEED_VERSION = "v1"
TRUCK_COUNT = 20_000
SEGMENTS = [f"SEG-{i:02d}" for i in range(13)]          # SEG-00 .. SEG-12
CALIBRATION_FROM = date(2026, 7, 20)
CALIBRATION_TO = date(2026, 7, 26)
ANCHOR_DATE = date(2026, 7, 20)
ANCHOR_IN_TEU = 6_462
ANCHOR_OUT_TEU = 11_490
ANCHOR_TOTAL_TEU = 17_952
STATES = ["APPROACHING", "QUEUED", "AT_GATE", "ON_CORRIDOR", "DEPARTED"]
CALIBRATION_NOTE = (
    "IN/OUT split scaled from published gate moves for 20-07-2026 "
    f"({ANCHOR_IN_TEU} IN / {ANCHOR_OUT_TEU} OUT / {ANCHOR_TOTAL_TEU} total TEU). "
    "Per-truck placement is deterministic from sha256(seed||index); no real "
    "per-truck GPS exists for the demo window, so all traffic here is SIMULATED."
)

# Replay clock: fixed start, so replay_ts is reproducible (no now()).
REPLAY_START = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)
REPLAY_WINDOW_MIN = 7 * 24 * 60          # the calibration week


def config() -> Dict[str, object]:
    """Everything that determines the output. Hashed as the reproducibility key."""
    return {
        "run_id": RUN_ID, "corridor": CORRIDOR, "seed": SEED,
        "seed_version": SEED_VERSION, "truck_count": TRUCK_COUNT,
        "segments": SEGMENTS, "states": STATES,
        "calibration_from": CALIBRATION_FROM.isoformat(),
        "calibration_to": CALIBRATION_TO.isoformat(),
        "anchor_date": ANCHOR_DATE.isoformat(),
        "anchor_in_teu": ANCHOR_IN_TEU, "anchor_out_teu": ANCHOR_OUT_TEU,
        "anchor_total_teu": ANCHOR_TOTAL_TEU,
        "replay_start": REPLAY_START.isoformat(),
        "replay_window_min": REPLAY_WINDOW_MIN,
    }


def config_sha256() -> str:
    return hashlib.sha256(
        json.dumps(config(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _h(index: int, field: str) -> int:
    """Stable per-truck, per-field integer. sha256 so it is machine-independent."""
    return int(hashlib.sha256(f"{SEED}|{index}|{field}".encode("utf-8")).hexdigest(), 16)


def build_trucks() -> List[Dict[str, object]]:
    """Generate the frozen population. Pure: no I/O, no clock, no RNG."""
    # Reproduce the anchor day's real IN/OUT ratio in the generated population.
    in_share = ANCHOR_IN_TEU / ANCHOR_TOTAL_TEU
    in_target = round(TRUCK_COUNT * in_share)

    # Deterministic AND exact: rank every truck by a stable hash and take the
    # first in_target as IN. A hash-modulo threshold would only approximate the
    # ratio (it drifts by ~1%); ranking hits in_target on the nose, so the
    # generated split provably matches the published anchor split.
    in_set = set(sorted(range(TRUCK_COUNT), key=lambda i: _h(i, "dir"))[:in_target])

    rows: List[Dict[str, object]] = []
    for i in range(TRUCK_COUNT):
        direction = "IN" if i in in_set else "OUT"
        segment = SEGMENTS[_h(i, "seg") % len(SEGMENTS)]
        state = STATES[_h(i, "state") % len(STATES)]
        minute = _h(i, "ts") % REPLAY_WINDOW_MIN
        rows.append({
            "run_id": RUN_ID,
            "truck_uid": f"{RUN_ID}:{i:05d}",
            # Synthetic plate — deliberately in a reserved-looking series so it
            # cannot collide with a real corpus plate.
            "truck_no": f"SIM{_h(i, 'plate') % 10_000_000:07d}",
            "segment_code": segment,
            "direction": direction,
            "state": state,
            "replay_ts": REPLAY_START + timedelta(minutes=minute),
        })
    return rows


class SeedError(RuntimeError):
    pass


async def run(dsn: str, dry_run: bool) -> int:
    trucks = build_trucks()
    sha = config_sha256()

    by_dir: Dict[str, int] = {}
    by_seg: Dict[str, int] = {}
    for t in trucks:
        by_dir[str(t["direction"])] = by_dir.get(str(t["direction"]), 0) + 1
        by_seg[str(t["segment_code"])] = by_seg.get(str(t["segment_code"]), 0) + 1

    print(f"run_id          {RUN_ID}")
    print(f"corridor        {CORRIDOR}")
    print(f"seed            {SEED}  ({SEED_VERSION})")
    print(f"config_sha256   {sha}")
    print(f"trucks          {len(trucks)}")
    print(f"segments        {len(by_seg)}  {min(by_seg)}..{max(by_seg)}")
    print(f"direction       IN={by_dir.get('IN', 0)}  OUT={by_dir.get('OUT', 0)}  "
          f"(anchor ratio IN={ANCHOR_IN_TEU / ANCHOR_TOTAL_TEU:.4f}, "
          f"generated IN={by_dir.get('IN', 0) / len(trucks):.4f})")
    print("segment distribution: " + ", ".join(f"{s}={by_seg[s]}" for s in sorted(by_seg)))

    if dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    engine = create_async_engine(dsn, pool_pre_ping=True)
    async with engine.begin() as conn:
        db = (await conn.execute(text("SELECT current_database()"))).scalar()
        expected = (os.environ.get("UC3_TARGET_DB") or "jnpa_qa").strip()
        if db != expected:
            raise SeedError(
                f"refusing to write: connected to database {db!r}, expected {expected!r}. "
                f"Set UC3_TARGET_DB to the database you intend to write to.")

        await conn.execute(text(
            "INSERT INTO core.sim_run (run_id, corridor, seed, seed_version, "
            "  config_sha256, truck_count, segment_count, calibration_from, "
            "  calibration_to, anchor_date, anchor_in_teu, anchor_out_teu, "
            "  anchor_total_teu, calibration_note) "
            "VALUES (:run_id, :corridor, :seed, :seed_version, :sha, :n, :segs, "
            "  :cfrom, :cto, :anchor, :ain, :aout, :atot, :note) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            "  config_sha256 = EXCLUDED.config_sha256, "
            "  truck_count = EXCLUDED.truck_count, "
            "  segment_count = EXCLUDED.segment_count, "
            "  calibration_note = EXCLUDED.calibration_note"),
            {"run_id": RUN_ID, "corridor": CORRIDOR, "seed": SEED,
             "seed_version": SEED_VERSION, "sha": sha, "n": len(trucks),
             "segs": len(SEGMENTS), "cfrom": CALIBRATION_FROM, "cto": CALIBRATION_TO,
             "anchor": ANCHOR_DATE, "ain": ANCHOR_IN_TEU, "aout": ANCHOR_OUT_TEU,
             "atot": ANCHOR_TOTAL_TEU, "note": CALIBRATION_NOTE})

        CHUNK = 1000
        for start in range(0, len(trucks), CHUNK):
            await conn.execute(text(
                "INSERT INTO core.sim_truck (run_id, truck_uid, truck_no, "
                "  segment_code, direction, state, replay_ts) "
                "VALUES (:run_id, :truck_uid, :truck_no, :segment_code, "
                "  :direction, :state, :replay_ts) "
                "ON CONFLICT (run_id, truck_uid) DO UPDATE SET "
                "  truck_no = EXCLUDED.truck_no, "
                "  segment_code = EXCLUDED.segment_code, "
                "  direction = EXCLUDED.direction, "
                "  state = EXCLUDED.state, "
                "  replay_ts = EXCLUDED.replay_ts"), trucks[start:start + CHUNK])

    await engine.dispose()
    print(f"\nseed complete: trucks={len(trucks)} sha256={sha}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=os.environ.get("POSTGRES_DSN", ""))
    ap.add_argument("--dry-run", action="store_true",
                    help="generate and report; write nothing")
    args = ap.parse_args()
    if not args.dry_run and not args.dsn:
        print("POSTGRES_DSN is required", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args.dsn, args.dry_run))
    except SeedError as exc:
        print(f"\nSEED REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
