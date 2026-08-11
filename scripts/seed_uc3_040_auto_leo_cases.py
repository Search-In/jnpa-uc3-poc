#!/usr/bin/env python3
"""Seed the Auto-LEO demonstration cases on top of the REAL Form 13s (UC3-040).

WHY THIS EXISTS
---------------
Tender UC3-R5 asks for the four-way join — e-seal, Form 13, weighbridge, ICEGATE
— and the ticket asks for "seeded pass and fail cases each raise the right flag".
The gate-data seed already generates 200 fully synthetic containers, but none of
them is anchored to a document the customer actually supplied, so the board could
not show the join working on JNPA's own paperwork.

This script builds the cases around the REAL Form 13s already imported into
core.gate_document by scripts/import_gate_documents.py. The declared weight, the
container number, the truck and the customs seal on each case are read FROM THAT
DOCUMENT — none of them is invented here.

WHAT IS REAL AND WHAT IS NOT
----------------------------
  REAL       the Form 13 itself: container, truck, VGM, customs seal, terminal.
  SIMULATED  the weighbridge reading, the e-seal read and the ICEGATE LEO status.
             Those three feeds do not exist in the corpus (gap register G8/G10).
             They are written with source_mode='sim' and the board labels them
             SIMULATED, so no evaluator can mistake them for captured events.

THE CASES
---------
Each case exercises exactly one outcome, so a flag on the board can be traced to
the condition that produced it:

  1. MEDU1777575  weighbridge 29,600 vs VGM 29,350 (0.85%)  -> leo_ready GREEN
  2. FFAU4770682  weighbridge 31,000 vs VGM 31,860 (2.70%)  -> WEIGHT_MISMATCH
  3. BMOU5841115  weighbridge FAILED, no reading at all      -> WEIGHT_MISSING (X4)
  4. SEGU1441550  ICEGATE has not granted the LEO            -> LEO_MISSING
  5. MEDU1777575' e-seal reports a tamper condition          -> ESEAL_TAMPER
     (applied only with --tamper, so case 1 stays green by default)

Idempotent: every capture is keyed on (container_no, capture_type, captured_at)
and re-running updates nothing it did not write. Nothing existing is deleted.

Usage:
    .venv/bin/python scripts/seed_uc3_040_auto_leo_cases.py --dry-run
    .venv/bin/python scripts/seed_uc3_040_auto_leo_cases.py
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

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: How each REAL Form 13 is exercised. ``weighbridge_kg`` is the SIMULATED
#: reading; None means the weighbridge failed and produced nothing (X4).
CASES: Dict[str, Dict[str, Any]] = {
    "form13_nsict_egate": {
        "outcome": "LEO_READY",
        "weighbridge_kg": 29600,      # 0.85% against the real 29,350 VGM -> within 2%
        "leo_status": "GRANTED",
        "tamper": False,
    },
    "form13_igt_eir": {
        "outcome": "WEIGHT_MISMATCH",
        "weighbridge_kg": 31000,      # 2.70% against the real 31,860 VGM -> over 2%
        "leo_status": "GRANTED",
        "tamper": False,
    },
    "form13_psa_bmct": {
        "outcome": "WEIGHT_MISSING",
        "weighbridge_kg": None,       # X4: the weighbridge failed
        "leo_status": "GRANTED",
        "tamper": False,
        "failed_wb_id": "WB-BMCT-02",
        "alternate_wb_id": "WB-BMCT-01",
    },
    "form13_nsft_eadvice": {
        "outcome": "LEO_MISSING",
        "weighbridge_kg": 26800,
        "leo_status": "PENDING",      # ICEGATE has not granted
        "tamper": False,
    },
}

SOURCE_MODE = "sim"       # the three simulated streams
TAMPER_VARIANT = "form13_nsict_egate"


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


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    ap.add_argument("--tamper", action="store_true",
                    help="also flip case 1's e-seal to TAMPERED (ESEAL_TAMPER demo)")
    args = ap.parse_args()

    import asyncpg

    conn = await asyncpg.connect(_dsn(), ssl="require", timeout=20)
    try:
        docs = await conn.fetch(
            """
            SELECT doc_variant, doc_ref, container_no, vehicle_no, seal1, seal2,
                   gross_weight_kg, doc_ts, terminal_id
              FROM core.gate_document
             WHERE doc_category = 'FORM13' AND data_origin = 'REAL'
             ORDER BY doc_variant
            """
        )
        if not docs:
            print("no REAL Form 13 documents in core.gate_document — run "
                  "scripts/import_gate_documents.py first")
            return 1

        rows: List[Dict[str, Any]] = []
        reroutes: List[Dict[str, Any]] = []
        for d in docs:
            variant = d["doc_variant"]
            case = CASES.get(variant)
            container = d["container_no"]
            if not case or not container:
                print(f"  skip {variant}: "
                      f"{'no case defined' if not case else 'document prints no container'}")
                continue

            declared = int(d["gross_weight_kg"]) if d["gross_weight_kg"] is not None else None
            plate = d["vehicle_no"]
            at = d["doc_ts"] or dt.datetime.now(dt.timezone.utc)
            tamper = case["tamper"] or (args.tamper and variant == TAMPER_VARIANT)

            # --- Form 13 (REAL values, carried verbatim from the document) ----
            rows.append({
                "capture_type": "FORM13", "container_no": container,
                "vehicle_plate": plate, "status": "REGISTERED",
                "captured_at": at, "source_mode": "live",
                "payload": {
                    "form13_no": d["doc_ref"],
                    "container_no": container,
                    "gross_wt_kg": declared,
                    "custom_seal_no": d["seal2"],
                    "line_seal_no": d["seal1"],
                    "source_document": variant,
                    "data_origin": "REAL",
                    "uc3_040_case": case["outcome"],
                },
            })

            # --- e-seal (SIMULATED) -------------------------------------------
            rows.append({
                "capture_type": "ESEAL", "container_no": container,
                "vehicle_plate": plate,
                "status": "TAMPERED" if tamper else "ARMED",
                "captured_at": at, "source_mode": SOURCE_MODE,
                "payload": {
                    # The e-seal id is the customs seal the REAL document prints,
                    # so the simulated read is at least about the right seal.
                    "eseal_id": d["seal2"] or d["seal1"],
                    "container_no": container,
                    "status": "TAMPERED" if tamper else "ARMED",
                    "tamper_flag": bool(tamper),
                    "simulated": True,
                    "gap_ref": "G8/G10",
                },
            })

            # --- weighbridge (SIMULATED; absent entirely for the X4 case) -----
            if case["weighbridge_kg"] is not None:
                rows.append({
                    "capture_type": "WEIGHBRIDGE", "container_no": container,
                    "vehicle_plate": plate, "status": "WEIGHED",
                    "captured_at": at, "source_mode": SOURCE_MODE,
                    "payload": {
                        "container_no": container, "vehicle_plate": plate,
                        "measured_wt_kg": case["weighbridge_kg"],
                        "declared_wt_kg": declared,
                        "axle_count": 5, "simulated": True, "gap_ref": "G8",
                    },
                })
            else:
                reroutes.append({
                    "container_no": container, "vehicle_plate": plate,
                    "failed_wb_id": case["failed_wb_id"],
                    "alternate_wb_id": case["alternate_wb_id"],
                })

            # --- ICEGATE (SIMULATED) ------------------------------------------
            rows.append({
                "capture_type": "ICEGATE", "container_no": container,
                "vehicle_plate": plate, "status": case["leo_status"],
                "captured_at": at, "source_mode": SOURCE_MODE,
                "payload": {
                    "container_no": container,
                    "leo_status": case["leo_status"],
                    "leo_granted": case["leo_status"] == "GRANTED",
                    "assessment": "FACILITATED",
                    "simulated": True, "gap_ref": "G10",
                },
            })

            wb = case["weighbridge_kg"]
            pct = (abs(wb - declared) / declared * 100.0) if (wb and declared) else None
            print(f"  {variant:22s} {container:12s} declared={declared or '—':>7} "
                  f"weighbridge={wb if wb is not None else 'FAILED':>7} "
                  f"{f'({pct:.2f}%)' if pct is not None else '':>9} -> {case['outcome']}")

        if args.dry_run:
            print(f"\n--dry-run: would write {len(rows)} captures, "
                  f"{len(reroutes)} weighbridge reroutes")
            return 0

        written = 0
        for r in rows:
            res = await conn.execute(
                """
                INSERT INTO core.gate_capture
                       (capture_type, container_no, vehicle_plate, source_mode,
                        status, captured_at, payload)
                VALUES ($1, $2, $3, $4, $5, $6, CAST($7 AS jsonb))
                ON CONFLICT (container_no, capture_type, captured_at)
                DO UPDATE SET payload = EXCLUDED.payload,
                              status = EXCLUDED.status,
                              vehicle_plate = EXCLUDED.vehicle_plate
                """,
                r["capture_type"], r["container_no"], r["vehicle_plate"],
                r["source_mode"], r["status"], r["captured_at"],
                json.dumps(r["payload"]),
            )
            written += 1

        for rr in reroutes:
            await conn.execute(
                """
                INSERT INTO core.weighbridge_reroute
                       (container_no, vehicle_plate, failed_wb_id, alternate_wb_id,
                        reason, customs_notified, notified_at)
                SELECT $1, $2, $3, $4, 'WEIGHBRIDGE_FAULT', true, now()
                 WHERE NOT EXISTS (
                       SELECT 1 FROM core.weighbridge_reroute
                        WHERE container_no = $1 AND failed_wb_id = $3)
                """,
                rr["container_no"], rr["vehicle_plate"],
                rr["failed_wb_id"], rr["alternate_wb_id"],
            )

        print(f"\nwrote {written} captures, {len(reroutes)} weighbridge reroute(s)")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
