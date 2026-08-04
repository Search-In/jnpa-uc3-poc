#!/usr/bin/env python3
"""Seed demonstrable export-leg data: booking -> Form13 -> gate-in -> VGM -> LEO -> COPRAR -> loaded.

The 2026-08-04 audit found ``core.export_booking`` and ``core.export_booking_event``
EMPTY on RDS. Migration 0115 created the tables and the full API exists
(``/api/export/*``, 12 endpoints), but nothing had ever written a row — so the
export leg rendered as `{"items": [], "total": 0}` and the whole feature was
invisible in the demo.

This seeds through the REAL service (``services.export_lifecycle``), never with
raw INSERTs. That matters:

  * the state machine is genuinely exercised (an illegal transition would fail
    here, so the seed doubles as a live check of the ordering rules),
  * ``core.export_booking_event`` gets a truthful step history with timestamps
    and actors, rather than a fabricated one,
  * ``core.cargo.lifecycle_status`` is advanced by the same code path the API
    uses, so import and export views agree,
  * every step publishes on the lifecycle bus exactly as a real one would.

Timestamps are back-dated so the chain looks like a plausible 3-day export cycle
rather than seven events in the same second.

    python scripts/seed_export_lifecycle.py --dsn "$POSTGRES_DSN"
    python scripts/seed_export_lifecycle.py --dry-run     # show the plan
    python scripts/seed_export_lifecycle.py --purge       # remove seeded rows

Idempotent: a booking_no that already exists is skipped, so re-running tops up
rather than duplicating.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Everything seeded here carries this prefix so --purge can find it and an
# operator can tell demo data from real data at a glance.
SEED_PREFIX = "DEMO-EXP"
SEED_ACTOR = "seed_export_lifecycle"


def _utc(days_ago: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


# Real ISO-6346-valid container numbers with NEUTRAL owner prefixes. Deliberately
# not MSCU/MAEU/CSNU: inventing a booking against a real carrier's prefix is the
# fixture-integrity problem the audit flagged separately (task D-12).
#
# Each scenario stops at a different stage, so the /export screen shows a
# realistic spread across the pipeline rather than seven identical rows.
SCENARIOS: list[dict[str, Any]] = [
    {
        "key": "loaded",
        "stop_after": "loaded",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0001", container_number="JNPU2200002",
            shipping_line="DEMO LINER ALPHA", vessel_name="DEMO EXPRESS",
            voyage_no="042E", via_no="R2971", pod="INMUN1", terminal="BMCT",
            cfs_code="CFS-DEMO-01", declared_gross_kg=24_500.0,
        ),
        "form13_no": f"{SEED_PREFIX}-F13-0001",
        "gate": {"gate_id": "GATE-3", "truck_no": "MH04AB1234"},
        "vgm": {"vgm_kg": 24_610.0, "method": "METHOD_1"},   # +0.45%, within tolerance
        "leo": {"leo_no": f"{SEED_PREFIX}-LEO-0001", "shipping_bill_no": f"{SEED_PREFIX}-SB-0001"},
        "coprar_ref": f"{SEED_PREFIX}-COPRAR-0001",
        "stowage_position": "0180-04-06",
        "days_ago": 3.0,
    },
    {
        # Stops at LEO — the box is customs-cleared but not yet on a load list.
        "key": "leo",
        "stop_after": "leo",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0002", container_number="JNPU2200018",
            shipping_line="DEMO LINER BETA", vessel_name="DEMO VOYAGER",
            voyage_no="017W", via_no="R2972", pod="AEJEA", terminal="NSIGT",
            cfs_code="CFS-DEMO-02", declared_gross_kg=21_000.0,
        ),
        "form13_no": f"{SEED_PREFIX}-F13-0002",
        "gate": {"gate_id": "GATE-1", "truck_no": "MH05CD4567"},
        "vgm": {"vgm_kg": 21_180.0, "method": "METHOD_2"},   # +0.86%
        "leo": {"leo_no": f"{SEED_PREFIX}-LEO-0002", "shipping_bill_no": f"{SEED_PREFIX}-SB-0002"},
        "days_ago": 1.5,
    },
    {
        # VGM MISMATCH: 26 400 vs 22 000 declared = +20%, far outside the 2%
        # SOLAS tolerance. Seeded ON PURPOSE — detecting a planted discrepancy is
        # itself scored, and the flag must be visible on screen.
        "key": "vgm_mismatch",
        "stop_after": "vgm",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0003", container_number="JNPU2200023",
            shipping_line="DEMO LINER GAMMA", vessel_name="DEMO MARINER",
            voyage_no="005E", via_no="R2973", pod="SGSIN", terminal="APMT",
            cfs_code="CFS-DEMO-03", declared_gross_kg=22_000.0,
        ),
        "form13_no": f"{SEED_PREFIX}-F13-0003",
        "gate": {"gate_id": "GATE-2", "truck_no": "MH46EF8901"},
        "vgm": {"vgm_kg": 26_400.0, "method": "METHOD_1"},
        "days_ago": 0.8,
    },
    {
        # In the yard awaiting VGM.
        "key": "gate_in",
        "stop_after": "gate_in",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0004", container_number="JNPU2200039",
            shipping_line="DEMO LINER ALPHA", vessel_name="DEMO EXPRESS",
            voyage_no="043E", via_no="R2974", pod="NLRTM", terminal="BMCT",
            cfs_code="CFS-DEMO-01", declared_gross_kg=18_750.0,
        ),
        "form13_no": f"{SEED_PREFIX}-F13-0004",
        "gate": {"gate_id": "GATE-3", "truck_no": "MH43BX1488"},
        "days_ago": 0.4,
    },
    {
        # Gate pass issued, truck not yet arrived.
        "key": "form13",
        "stop_after": "form13",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0005", container_number="JNPU2200044",
            shipping_line="DEMO LINER BETA", vessel_name="DEMO VOYAGER",
            voyage_no="018W", via_no="R2975", pod="AEJEA", terminal="NSIGT",
            cfs_code="CFS-DEMO-02", declared_gross_kg=26_100.0,
        ),
        "form13_no": f"{SEED_PREFIX}-F13-0005",
        "days_ago": 0.2,
    },
    {
        # Freshly booked — nothing has happened yet.
        "key": "booked",
        "stop_after": "booked",
        "booking": dict(
            booking_no=f"{SEED_PREFIX}-0006", container_number="JNPU2200050",
            shipping_line="DEMO LINER GAMMA", vessel_name="DEMO MARINER",
            voyage_no="006E", via_no="R2976", pod="SGSIN", terminal="APMT",
            cfs_code="CFS-DEMO-03", declared_gross_kg=20_400.0,
        ),
        "days_ago": 0.1,
    },
]

#: Ordered stages, so `stop_after` reads declaratively above.
_ORDER = ["booked", "form13", "gate_in", "vgm", "leo", "load_listed", "loaded"]


def _reaches(scenario: dict, stage: str) -> bool:
    return _ORDER.index(stage) <= _ORDER.index(scenario["stop_after"])


async def seed_one(svc, s: dict) -> dict[str, Any]:
    """Drive one booking through its scenario. Returns a short report."""
    from services.export_lifecycle.service import ExportValidationError

    booking_no = s["booking"]["booking_no"]
    base_days = s["days_ago"]

    try:
        row = await svc.create_booking(**s["booking"], created_by=SEED_ACTOR)
    except ExportValidationError as exc:
        if getattr(exc, "code", "") in {"booking_already_exists", "container_already_booked"}:
            return {"booking_no": booking_no, "skipped": "already exists"}
        raise
    bid = row["id"]
    reached = "booked"

    # Back-date each step so the history spans the scenario's window instead of
    # collapsing into one instant.
    def at(fraction: float) -> datetime:
        return _utc(base_days * (1.0 - fraction))

    if _reaches(s, "form13") and s.get("form13_no"):
        await svc.issue_form13(bid, form13_no=s["form13_no"], issued_at=at(0.15),
                               actor=SEED_ACTOR, actor_role="TERMINAL_OPS")
        reached = "form13"
    if _reaches(s, "gate_in") and s.get("gate"):
        await svc.gate_in(bid, **s["gate"], occurred_at=at(0.35),
                          actor=SEED_ACTOR, actor_role="TERMINAL_OPS")
        reached = "gate_in"
    if _reaches(s, "vgm") and s.get("vgm"):
        res = await svc.capture_vgm(bid, **s["vgm"], captured_at=at(0.55),
                                    actor=SEED_ACTOR, actor_role="TERMINAL_OPS")
        reached = "vgm"
        if res.get("vgm_flag"):
            # Expected for the planted-mismatch scenario; surfaced, never hidden.
            print(f"    ! {booking_no}: {res['vgm_flag']} "
                  f"({res.get('vgm_variance_pct')}% vs {res.get('vgm_tolerance_pct')}% tolerance)")
    if _reaches(s, "leo") and s.get("leo"):
        await svc.grant_leo(bid, **s["leo"], granted_at=at(0.75),
                            actor=SEED_ACTOR, actor_role="CUSTOMS")
        reached = "leo"
    if _reaches(s, "load_listed") and s.get("coprar_ref"):
        await svc.add_to_load_list(bid, coprar_ref=s["coprar_ref"], listed_at=at(0.9),
                                   actor=SEED_ACTOR, actor_role="TERMINAL_OPS")
        reached = "load_listed"
    if _reaches(s, "loaded"):
        await svc.confirm_loaded(bid, stowage_position=s.get("stowage_position"),
                                 loaded_at=at(1.0),
                                 actor=SEED_ACTOR, actor_role="TERMINAL_OPS")
        reached = "loaded"

    return {"booking_no": booking_no, "id": bid,
            "container": s["booking"].get("container_number"), "reached": reached}


async def purge(dsn: str) -> None:
    """Remove everything this script seeded (booking_no LIKE 'DEMO-EXP%')."""
    from jnpa_shared.db import execute, fetch_all

    rows = await fetch_all(
        "SELECT id, booking_no FROM core.export_booking WHERE booking_no LIKE :p",
        {"p": f"{SEED_PREFIX}%"}, dsn=dsn)
    if not rows:
        print("nothing to purge")
        return
    ids = [r["id"] for r in rows]
    await execute(
        "DELETE FROM core.export_booking_event WHERE booking_id = ANY(:ids)",
        {"ids": ids}, dsn=dsn)
    await execute(
        "DELETE FROM core.export_booking WHERE id = ANY(:ids)", {"ids": ids}, dsn=dsn)
    print(f"purged {len(ids)} seeded booking(s) and their events")


def resolve_dsn(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    for var in ("POSTGRES_DSN", "RFID_POSTGRES_DSN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    env_file = REPO_ROOT / ".env.local"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if line.startswith("POSTGRES_DSN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("no DSN — pass --dsn or set POSTGRES_DSN")


async def amain(args) -> int:
    if args.dry_run:
        print(f"would seed {len(SCENARIOS)} export booking(s):")
        for s in SCENARIOS:
            print(f"  {s['booking']['booking_no']}  {s['booking']['container_number']}"
                  f"  -> {s['stop_after']}")
        return 0

    dsn = resolve_dsn(args.dsn)
    # asyncpg needs the +asyncpg driver prefix that jnpa_shared.db expects.
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
        dsn = dsn.replace("?sslmode=require", "?ssl=require")
    print(f"==> {re.sub(r'://([^:/@]+):[^@]*@', r'://\\1:****@', dsn)}")

    if args.purge:
        await purge(dsn)
        return 0

    from services.export_lifecycle import ExportLifecycleService

    svc = ExportLifecycleService(dsn=dsn)
    results = []
    for s in SCENARIOS:
        res = await seed_one(svc, s)
        results.append(res)
        if res.get("skipped"):
            print(f"  = {res['booking_no']}: {res['skipped']}")
        else:
            print(f"  + {res['booking_no']} ({res['container']}) -> {res['reached']}")

    summary = await svc.summary()
    print("\n==> /api/export/summary now reports:")
    for k, v in sorted(summary.items()):
        print(f"      {k}: {v}")
    seeded = [r for r in results if not r.get("skipped")]
    print(f"\n==> seeded {len(seeded)} booking(s); {len(results) - len(seeded)} already present")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dsn", help="POSTGRES_DSN override")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    ap.add_argument("--purge", action="store_true", help="delete seeded DEMO-EXP* rows")
    args = ap.parse_args(argv)
    return asyncio.run(amain(args))


if __name__ == "__main__":
    raise SystemExit(main())
