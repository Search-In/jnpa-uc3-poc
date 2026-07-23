#!/usr/bin/env python3
"""Idempotent importer for UC-III Module 12 — Performance & Daily Reports.

Parses the OFFICIAL JNPA PDF reports (no dummy / generated data — every value
comes from the source PDF) into the ADDITIVE tables from migration 0028:

  * Daily Status Report  (…/Daily Status Report *.pdf)  -> perf_daily_*
      (A) Container TEUs + (C) Rail  -> jnpa.perf_daily_traffic
      (B) Tonnage                    -> jnpa.perf_daily_tonnage
      (D/E/F/G) Pendency/Yard/Gate/Reefer -> jnpa.perf_daily_terminal_status
      (H) Vessels Under Operation    -> jnpa.perf_daily_vessels
  * FY JN Port TEUs      (…*TEU*.pdf)  -> jnpa.perf_monthly_teu
  * NLDS/LDB Analytics   (…*LDB*.pdf)  -> jnpa.perf_ldb_*
      port dwell / facility dwell / congestion / route modal-share / weather

Purely additive — it NEVER touches cargo / cfs_ecy / vehicle / driver /
transporter / auth / ldb_movements tables. Terminal labels are normalised to the
canonical codes (GTI≡APMT, BMCTPL≡BMCT, …) via the jnpa.perf_terminals dimension.

Idempotency: every insert is ON CONFLICT DO NOTHING against the migration-0028
UNIQUE constraints, so re-runs report 0 new rows.

Usage:
    .venv/bin/python scripts/import_performance_reports.py --kind all
    .venv/bin/python scripts/import_performance_reports.py --kind daily --dry-run
Options: --data-dir PATH, --dsn, --kind {all,daily,monthly,ldb}, --dry-run,
         --limit N (daily files), --no-ensure.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "shared"))

DEFAULT_DATA_DIR = "/Users/pandurangdhage/Downloads/Digital Twin/Data/12-Performance & Daily Reports"
DEFAULT_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres",
)
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
BATCH = 500

# The PDF parsing engine now lives in the service layer so the interactive client
# upload flow (services.performance.upload_service) and this offline backfill share
# ONE validated implementation. Imported — not duplicated — to prevent drift.
from services.performance.pdf_parsers import (          # noqa: E402
    MONTHS, num, as_int, pct, norm_terminal, parse_dt,
    daily_date_from_name, ldb_month_from_name,
    parse_daily as _parse_daily, parse_monthly as _parse_monthly, parse_ldb as _parse_ldb,
)


# Path-based wrappers keeping this script's original call signatures intact.
def parse_daily(path: Path):
    return _parse_daily(path, path.name)


def parse_monthly(path: Path):
    return _parse_monthly(path, path.name)


def parse_ldb(path: Path):
    return _parse_ldb(path, path.name)



# =====================================================================
# DB writers (idempotent, ON CONFLICT DO NOTHING)
# =====================================================================
_INSERTS = {
    "snapshot": """INSERT INTO jnpa.perf_daily_snapshot (report_date, as_of_ts, source_file)
        VALUES (:report_date, :as_of_ts, :source_file)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_snapshot DO NOTHING""",
    "traffic": """INSERT INTO jnpa.perf_daily_traffic
        (report_date, terminal_code, period, vessels, imp_teus, exp_teus, total_teus,
         rakes, rail_dis_teus, rail_ldg_teus, rail_total_teus)
        VALUES (:report_date,:terminal_code,:period,:vessels,:imp_teus,:exp_teus,:total_teus,
                :rakes,:rail_dis_teus,:rail_ldg_teus,:rail_total_teus)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_traffic DO NOTHING""",
    "tonnage": """INSERT INTO jnpa.perf_daily_tonnage
        (report_date, category, period, vessels, liquid_tonnes, dry_bulk_tonnes,
         break_bulk_tonnes, total_tonnes)
        VALUES (:report_date,:category,:period,:vessels,:liquid_tonnes,:dry_bulk_tonnes,
                :break_bulk_tonnes,:total_tonnes)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_tonnage DO NOTHING""",
    "status": """INSERT INTO jnpa.perf_daily_terminal_status
        (report_date, terminal_code, icd_pendency_teus, cfs_pendency_teus, yard_import_teus,
         yard_export_teus, yard_transhipment_teus, yard_total_teus, yard_usable_capacity_teus,
         yard_occupancy_pct, gate_in_teus, gate_out_teus, gate_total_teus,
         reefer_total_slots, reefer_occupied_slots, reefer_available_slots)
        VALUES (:report_date,:terminal_code,:icd_pendency_teus,:cfs_pendency_teus,:yard_import_teus,
                :yard_export_teus,:yard_transhipment_teus,:yard_total_teus,:yard_usable_capacity_teus,
                :yard_occupancy_pct,:gate_in_teus,:gate_out_teus,:gate_total_teus,
                :reefer_total_slots,:reefer_occupied_slots,:reefer_available_slots)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_status DO NOTHING""",
    "vessels": """INSERT INTO jnpa.perf_daily_vessels
        (report_date, terminal_code, berth_no, via_no, vessel_name, cargo_commodity,
         berthed_on, expected_completion)
        VALUES (:report_date,:terminal_code,:berth_no,:via_no,:vessel_name,:cargo_commodity,
                :berthed_on,:expected_completion)
        ON CONFLICT ON CONSTRAINT uq_perf_daily_vessel DO NOTHING""",
    "monthly": """INSERT INTO jnpa.perf_monthly_teu
        (fiscal_year, month_date, year_label, month_label, terminal_code, vessel_calls,
         discharge_teus, load_teus, total_teus)
        VALUES (:fiscal_year,:month_date,:year_label,:month_label,:terminal_code,:vessel_calls,
                :discharge_teus,:load_teus,:total_teus)
        ON CONFLICT ON CONSTRAINT uq_perf_monthly_teu DO NOTHING""",
    "port_dwell": """INSERT INTO jnpa.perf_ldb_port_dwell
        (report_month, terminal_code, cycle, segment, dwell_hours, dwell_hours_prev)
        VALUES (:report_month,:terminal_code,:cycle,:segment,:dwell_hours,:dwell_hours_prev)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_port_dwell DO NOTHING""",
    "facility": """INSERT INTO jnpa.perf_ldb_facility_dwell
        (report_month, facility_type, facility_name, facility_name_norm, dwell_hours, dwell_hours_prev)
        VALUES (:report_month,:facility_type,:facility_name,:facility_name_norm,:dwell_hours,:dwell_hours_prev)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_facility_dwell DO NOTHING""",
    "congestion": """INSERT INTO jnpa.perf_ldb_congestion
        (report_month, cycle, cluster_no, cluster_name, cfs_count, pct_containers, congestion_level)
        VALUES (:report_month,:cycle,:cluster_no,:cluster_name,:cfs_count,:pct_containers,:congestion_level)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_congestion DO NOTHING""",
    "routes": """INSERT INTO jnpa.perf_ldb_route_movement
        (report_month, cycle, transport_mode, route_name, pct_share)
        VALUES (:report_month,:cycle,:transport_mode,:route_name,:pct_share)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_route DO NOTHING""",
    "weather": """INSERT INTO jnpa.perf_ldb_weather
        (report_month, terminal_code, cycle, weather, dwell_hours)
        VALUES (:report_month,:terminal_code,:cycle,:weather,:dwell_hours)
        ON CONFLICT ON CONSTRAINT uq_perf_ldb_weather DO NOTHING""",
}
# columns each insert expects (for filling missing keys with None)
_COLS = {
    "traffic": ("report_date", "terminal_code", "period", "vessels", "imp_teus", "exp_teus",
                "total_teus", "rakes", "rail_dis_teus", "rail_ldg_teus", "rail_total_teus"),
    "tonnage": ("report_date", "category", "period", "vessels", "liquid_tonnes",
                "dry_bulk_tonnes", "break_bulk_tonnes", "total_tonnes"),
    "status": ("report_date", "terminal_code", "icd_pendency_teus", "cfs_pendency_teus",
               "yard_import_teus", "yard_export_teus", "yard_transhipment_teus", "yard_total_teus",
               "yard_usable_capacity_teus", "yard_occupancy_pct", "gate_in_teus", "gate_out_teus",
               "gate_total_teus", "reefer_total_slots", "reefer_occupied_slots",
               "reefer_available_slots"),
    "vessels": ("report_date", "terminal_code", "berth_no", "via_no", "vessel_name",
                "cargo_commodity", "berthed_on", "expected_completion"),
}


async def _insert(conn, key: str, rows: List[Dict[str, Any]]) -> int:
    from sqlalchemy import text
    if not rows:
        return 0
    stmt = text(_INSERTS[key])
    cols = _COLS.get(key)
    inserted = 0
    for r in rows:
        params = {c: r.get(c) for c in cols} if cols else r
        res = await conn.execute(stmt, params)
        inserted += (res.rowcount or 0)
    return inserted


# =====================================================================
# ORCHESTRATION
# =====================================================================
def find_files(data_dir: Path) -> Dict[str, List[Path]]:
    daily, monthly, ldb = [], [], []
    for p in data_dir.rglob("*.pdf"):
        n = p.name.lower()
        if "daily" in n and "status" in n:
            daily.append(p)
        elif "teu" in n:
            monthly.append(p)
        elif "ldb" in n or "nlds" in n:
            ldb.append(p)
    daily.sort()
    return {"daily": daily, "monthly": sorted(monthly), "ldb": sorted(ldb)}


async def run(args) -> Dict[str, Any]:
    from jnpa_shared.db import get_engine
    data_dir = Path(args.data_dir)
    files = find_files(data_dir)
    counts: Counter = Counter()
    engine = get_engine(args.dsn)

    if not args.no_ensure and not args.dry_run:
        from gateway.performance_ext import ensure_performance_schema
        await ensure_performance_schema(args.dsn)

    # ---- DAILY ----
    if args.kind in ("all", "daily"):
        dailies = files["daily"]
        if args.limit:
            dailies = dailies[:args.limit]
        counts["daily_files"] = len(dailies)
        for path in dailies:
            parsed = parse_daily(path)
            if not parsed:
                counts["daily_skipped"] += 1
                continue
            counts["traffic"] += len(parsed["traffic"])
            counts["tonnage"] += len(parsed["tonnage"])
            counts["status"] += len(parsed["status"])
            counts["vessels"] += len(parsed["vessels"])
            if not args.dry_run:
                async with engine.begin() as conn:
                    from sqlalchemy import text
                    await conn.execute(text(_INSERTS["snapshot"]), {
                        "report_date": parsed["report_date"], "as_of_ts": parsed["as_of_ts"],
                        "source_file": parsed["source_file"]})
                    counts["ins_traffic"] += await _insert(conn, "traffic", parsed["traffic"])
                    counts["ins_tonnage"] += await _insert(conn, "tonnage", parsed["tonnage"])
                    counts["ins_status"] += await _insert(conn, "status", parsed["status"])
                    counts["ins_vessels"] += await _insert(conn, "vessels", parsed["vessels"])

    # ---- MONTHLY ----
    if args.kind in ("all", "monthly"):
        for path in files["monthly"]:
            rows = parse_monthly(path)
            counts["monthly"] += len(rows)
            if not args.dry_run:
                async with engine.begin() as conn:
                    counts["ins_monthly"] += await _insert(conn, "monthly", rows)

    # ---- LDB ----
    if args.kind in ("all", "ldb"):
        for path in files["ldb"]:
            data = parse_ldb(path)
            for k in ("port_dwell", "facility", "congestion", "routes", "weather"):
                counts[k] += len(data.get(k, []))
            if not args.dry_run:
                async with engine.begin() as conn:
                    for k in ("port_dwell", "facility", "congestion", "routes", "weather"):
                        counts[f"ins_{k}"] += await _insert(conn, k, data.get(k, []))

    return dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--kind", choices=["all", "daily", "monthly", "ldb"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None, help="max daily files")
    ap.add_argument("--no-ensure", action="store_true")
    args = ap.parse_args()

    counts = asyncio.run(run(args))

    print("\n" + "=" * 66)
    print("PERFORMANCE & DAILY REPORTS IMPORT" +
          ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 66)
    print(f"  kind                : {args.kind}")
    print(f"  daily files parsed  : {counts.get('daily_files', 0)}"
          f"  (skipped {counts.get('daily_skipped', 0)})")
    print("-" * 66)
    label = "parsed" if args.dry_run else "inserted(new)/parsed"
    def line(name, parsed_key, ins_key):
        if args.dry_run:
            print(f"  {name:<22}: {counts.get(parsed_key, 0)}")
        else:
            print(f"  {name:<22}: {counts.get(ins_key, 0)} / {counts.get(parsed_key, 0)}")
    print(f"  ({label})")
    line("daily traffic (A+C)", "traffic", "ins_traffic")
    line("daily tonnage (B)", "tonnage", "ins_tonnage")
    line("daily status (D-G)", "status", "ins_status")
    line("daily vessels (H)", "vessels", "ins_vessels")
    line("monthly TEU", "monthly", "ins_monthly")
    line("ldb port dwell", "port_dwell", "ins_port_dwell")
    line("ldb facility dwell", "facility", "ins_facility")
    line("ldb congestion", "congestion", "ins_congestion")
    line("ldb route movement", "routes", "ins_routes")
    line("ldb weather", "weather", "ins_weather")
    print("=" * 66 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
