#!/usr/bin/env python3
"""Reset the JNPA UC-III stack to a clean demo baseline (Prompt 12, Deliverable 5).

Run after the evaluator walks away. Wipes *ephemeral* data so the next demo
starts fresh, but **keeps trained models** (MinIO ``models`` bucket + the
``congestion-artifacts`` / ``anomaly-artifacts`` volumes) so the stack serves
instantly without the ~15-min first-boot retrain.

What it does (each step best-effort + logged; a failure in one does not abort
the rest):

  1. Reset any still-running what-if scenarios via the runner (releases gate
     closures, TAS reschedules, synthetic trucks, provisional alerts).
  2. TRUNCATE the ephemeral Timescale hypertables + operational event tables:
     anpr_reads, rfid_reads, truck_telemetry, traffic_snapshots, alerts,
     scenarios, scenario_handles, scenario_steps.
  3. Drop provisional (cure-window) rows from vehicle_master; keep verified RCs
     and the seed tables (gates, cameras, services, geofence_zones).
  4. Flush ephemeral Redis keys (gateway cache, reroute advisories, frame bus,
     queue pressure) — but NOT the whole DB, so any model pointers survive.

It deliberately does **not** run ``docker compose down -v`` (that would wipe the
model volumes). For a full teardown use ``make down``.

Usage:  python scripts/demo_reset.py            (from the repo root, stack up)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = "http://localhost:8400"

# Ephemeral tables truncated wholesale (RESTART IDENTITY for any serials).
EPHEMERAL_TABLES = [
    "core.anpr_read",
    "core.rfid_read",
    "core.truck_telemetry",
    "core.traffic_snapshot",
    "core.alert",
    "core.scenario_step",
    "core.scenario_handle",
    "core.scenario",
]

# Redis key globs we clear (ephemeral). We never FLUSHALL — that could drop a
# model/metrics pointer another service cached.
REDIS_GLOBS = [
    "jnpa:cache:*",        # gateway fallback cache (ANPR/Vahan/truck)
    "jnpa:reroute:*",      # PWA reroute advisories
    "jnpa:advisory:*",
    "frames.*",            # shared camera frame bus (Redis Streams)
    "jnpa:queue:*",        # gate queue pressure
    "jnpa:truck:*",        # sampled truck positions
]


def _compose() -> List[str]:
    env = REPO_ROOT / ".env.local"
    base = ["docker", "compose"]
    if env.is_file():
        base += ["--env-file", ".env.local"]
    return base


def _run(cmd: List[str], desc: str) -> bool:
    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {desc}: {exc!r}")
        return False
    if proc.returncode != 0:
        print(f"  ! {desc}: exit {proc.returncode}: {proc.stderr.strip()[-200:]}")
        return False
    out = proc.stdout.strip()
    print(f"  ✓ {desc}" + (f" — {out.splitlines()[-1]}" if out else ""))
    return True


def reset_scenarios() -> None:
    print("1. Resetting running what-if scenarios:")
    try:
        listing = httpx.get(f"{SCENARIOS}/scenarios", timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        print(f"  ! scenarios-runner not reachable ({exc!r}); skipping live reset")
        return
    handles = listing.get("handles", [])
    if not handles:
        print("  · no running scenarios")
        return
    with httpx.Client(timeout=60.0) as c:
        for h in handles:
            name, hid = h.get("name"), h.get("handle_id")
            if not (name and hid):
                continue
            try:
                r = c.post(f"{SCENARIOS}/scenarios/{name}/reset", json={"handle_id": hid})
                ok = r.status_code == 200 and r.json().get("ok") is True
                print(f"  {'✓' if ok else '!'} reset {name} ({hid})")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! reset {name} ({hid}): {exc!r}")


def truncate_tables() -> None:
    print("2. Truncating ephemeral Timescale tables:")
    # Truncate only tables that exist, and do each independently via a DO block so
    # a missing table (a DB predating a migration) never aborts the whole reset.
    statements = "; ".join(
        f"""DO $$ BEGIN
              IF to_regclass('{t}') IS NOT NULL THEN
                EXECUTE 'TRUNCATE {t} RESTART IDENTITY CASCADE';
              END IF;
            END $$"""
        for t in EPHEMERAL_TABLES
    )
    _run(_compose() + ["exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "postgres",
                       "-v", "ON_ERROR_STOP=0", "-c", statements],
         "truncate ephemeral tables (existing only)")


def drop_provisional() -> None:
    print("3. Dropping provisional vehicles (keeping verified RCs + seed tables):")
    sql = "DELETE FROM core.vehicle_rc WHERE provisional = true;"
    _run(_compose() + ["exec", "-T", "postgres", "psql", "-U", "postgres", "-d", "postgres",
                       "-c", sql], "delete provisional vehicle_master rows")


def flush_redis() -> None:
    print("4. Clearing ephemeral Redis keys (keeping model pointers):")
    # Use a single Lua-free pipeline: for each glob, SCAN+DEL via redis-cli.
    # `redis-cli --scan --pattern <glob> | xargs redis-cli del` inside the container.
    for glob in REDIS_GLOBS:
        script = (
            f"redis-cli --scan --pattern '{glob}' | "
            f"{{ xargs -r redis-cli del >/dev/null 2>&1 || true; }}; "
            f"echo cleared '{glob}'"
        )
        _run(_compose() + ["exec", "-T", "redis", "sh", "-c", script], f"redis del {glob}")


def main() -> int:
    print("JNPA UC-III — demo reset (ephemeral data wiped, trained models kept)\n")
    reset_scenarios()
    truncate_tables()
    drop_provisional()
    flush_redis()
    print("\n✓ Demo baseline restored. Trained models (MinIO + artifact volumes) untouched.")
    print("  The simulators (Vahan / ANPR / trucks / RFID) will re-populate live data on tick.")
    print("  For a full teardown (drops model volumes too): make down")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
