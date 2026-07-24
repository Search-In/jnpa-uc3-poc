#!/usr/bin/env python3
"""Idempotent importer for the Driver Master & PDP history (PDP Details.xlsx).

Loads two sheets into the ADDITIVE tables from migration 0026:
  * "Application Data" -> core.driver       (key: licence_no_norm)
  * "PDP Data"         -> core.pdp  (key: pdp_id)

Purely additive — it NEVER touches core.driver_identity / driver_enrollments /
device_bindings / driver_faces, so both driver login flows are unaffected.

driver_master rows resolve their transporter_id from core.transporter by
normalised company name (the Transport Master link). Cleaning:
  * licence_no_norm = UPPER + alnum-only
  * dob validated to a sane year range (else NULL, flagged)
  * name / company_name trimmed
A record is INVALID (not imported) only if it lacks a licence number or name.

Usage:
    python scripts/import_driver_master.py --dry-run          # parse+clean, no DB
    POSTGRES_DSN='postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres' \
        .venv/bin/python scripts/import_driver_master.py      # live upsert
Options: --xlsx PATH, --dsn, --dry-run, --limit N, --report PATH, --skip-pdp.
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

DEFAULT_XLSX = (
    "/Users/pandurangdhage/Downloads/Digital Twin/Data/11-Transport Data/PDP Details.xlsx"
)
DEFAULT_DSN = os.environ.get(
    "POSTGRES_DSN",
    "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres",
)
PDP_BATCH = 2000
DRIVER_BATCH = 1000


# --- normalization -----------------------------------------------------------
def norm_licence(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper()) or None


def clean_text(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    v = str(raw).strip()
    return v or None


def clean_date(raw: Any, *, min_year=1920, max_year=2015) -> Tuple[Optional[dt.date], bool]:
    """Return (date | None, was_bad). Accepts datetime/date/str."""
    if raw is None:
        return None, False
    d: Optional[dt.date] = None
    if isinstance(raw, dt.datetime):
        d = raw.date()
    elif isinstance(raw, dt.date):
        d = raw
    else:
        s = str(raw).strip()
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                d = dt.datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    if d is None:
        return None, True
    if not (min_year <= d.year <= max_year):
        return None, True  # out-of-range (e.g. the '0988' typo)
    return d, False


def clean_int(raw: Any) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return None


# --- driver_master rows ------------------------------------------------------
_APP_COLS = ("Srno", "company_name", "driver_name", "photo", "validity",
             "licence_number", "latest_pdp_number", "dob(YYYY-MM-DD)", "Licence type")


def clean_driver(raw: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    issues: List[str] = []
    licence_no = clean_text(raw.get("licence_number"))
    licence_norm = norm_licence(licence_no)
    name = clean_text(raw.get("driver_name"))
    if not licence_norm:
        return None, ["missing_licence"]
    if not name:
        return None, ["missing_name"]
    valid_to, bad_valid = clean_date(raw.get("validity"), min_year=2000, max_year=2100)
    if bad_valid:
        issues.append("validity_invalid")
    dob, bad_dob = clean_date(raw.get("dob(YYYY-MM-DD)"))
    if bad_dob:
        issues.append("dob_invalid")
    rec = {
        "licence_no": licence_no,
        "licence_no_norm": licence_norm,
        "source_srno": clean_int(raw.get("Srno")),
        "name": name,
        "company_name": clean_text(raw.get("company_name")),
        "photo_file": clean_text(raw.get("photo")),
        "licence_type": clean_text(raw.get("Licence type")) or "HMV",
        "licence_valid_to": valid_to,
        "latest_pdp_number": clean_text(raw.get("latest_pdp_number")),
        "dob": dob,
    }
    return rec, issues


_DRIVER_UPSERT = """
INSERT INTO core.driver AS d
    (licence_no, licence_no_norm, source_srno, name, company_name, transporter_id,
     photo_file, licence_type, licence_valid_to, latest_pdp_number, dob)
VALUES
    (:licence_no, :licence_no_norm, :source_srno, :name, :company_name, :transporter_id,
     :photo_file, :licence_type, :licence_valid_to, :latest_pdp_number, :dob)
ON CONFLICT (licence_no_norm) DO UPDATE SET
    licence_no = EXCLUDED.licence_no, source_srno = EXCLUDED.source_srno,
    name = EXCLUDED.name, company_name = EXCLUDED.company_name,
    transporter_id = EXCLUDED.transporter_id, photo_file = EXCLUDED.photo_file,
    licence_type = EXCLUDED.licence_type, licence_valid_to = EXCLUDED.licence_valid_to,
    latest_pdp_number = EXCLUDED.latest_pdp_number, dob = EXCLUDED.dob,
    updated_at = now()
WHERE (d.name, d.company_name, d.transporter_id, d.photo_file, d.licence_type,
       d.licence_valid_to, d.latest_pdp_number, d.dob, d.source_srno)
  IS DISTINCT FROM
      (EXCLUDED.name, EXCLUDED.company_name, EXCLUDED.transporter_id, EXCLUDED.photo_file,
       EXCLUDED.licence_type, EXCLUDED.licence_valid_to, EXCLUDED.latest_pdp_number,
       EXCLUDED.dob, EXCLUDED.source_srno)
RETURNING (xmax = 0) AS inserted
"""


# --- PDP history rows --------------------------------------------------------
def clean_pdp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pdp_id = clean_int(raw.get("pdp_id"))
    if pdp_id is None:
        return None
    validity, _ = clean_date(raw.get("validity"), min_year=2000, max_year=2100)
    return {
        "pdp_id": pdp_id,
        "acceptance_time_stamp": raw.get("acceptance_time_stamp"),
        "active": bool(raw.get("active")),
        "appl_number": clean_text(raw.get("appl_number")),
        "pdp_number": clean_text(raw.get("pdp_number")),
        "validity": validity,
        "remarks": clean_text(raw.get("remarks")),
        "pdp_cancelled_by": clean_text(raw.get("pdp_cancelled_by")),
        "cancellation_time": raw.get("cancellation_time"),
    }


_PDP_UPSERT = """
INSERT INTO core.pdp
    (pdp_id, acceptance_time_stamp, active, appl_number, pdp_number, validity,
     remarks, pdp_cancelled_by, cancellation_time)
VALUES
    (:pdp_id, :acceptance_time_stamp, :active, :appl_number, :pdp_number, :validity,
     :remarks, :pdp_cancelled_by, :cancellation_time)
ON CONFLICT (pdp_id) DO UPDATE SET
    acceptance_time_stamp = EXCLUDED.acceptance_time_stamp, active = EXCLUDED.active,
    appl_number = EXCLUDED.appl_number, pdp_number = EXCLUDED.pdp_number,
    validity = EXCLUDED.validity, remarks = EXCLUDED.remarks,
    pdp_cancelled_by = EXCLUDED.pdp_cancelled_by, cancellation_time = EXCLUDED.cancellation_time
"""


# --- workbook ----------------------------------------------------------------
def load_sheet(xlsx: str, sheet: str, cols, limit: Optional[int]) -> List[Dict[str, Any]]:
    import openpyxl

    wb = openpyxl.load_workbook(xlsx, data_only=True, read_only=True)
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    header = list(next(it))
    idx = {h: i for i, h in enumerate(header)}
    missing = [c for c in cols if c not in idx]
    if missing:
        wb.close()
        raise SystemExit(f"FATAL: {sheet} missing columns {missing}")
    out: List[Dict[str, Any]] = []
    for n, values in enumerate(it):
        if limit is not None and n >= limit:
            break
        out.append({c: (values[idx[c]] if idx[c] < len(values) else None) for c in cols})
    wb.close()
    return out


async def resolve_transporters(dsn: str) -> Dict[str, int]:
    from jnpa_shared.db import fetch_all

    rows = await fetch_all("SELECT id, name FROM core.transporter", {}, dsn=dsn)
    return {str(r["name"]).strip().lower(): int(r["id"]) for r in rows if r.get("name")}


async def import_drivers(records: List[Dict[str, Any]], name2id: Dict[str, int], dsn: str) -> Dict[str, int]:
    """Chunked upsert: one committed transaction per DRIVER_BATCH rows (far fewer
    round-trips than a transaction per row), keeping per-row RETURNING granularity."""
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    engine = get_engine(dsn)
    stmt = text(_DRIVER_UPSERT)
    tally = {"inserted": 0, "updated": 0, "skipped": 0, "transporter_linked": 0}
    prepared: List[Dict[str, Any]] = []
    for rec in records:
        cn = (rec.get("company_name") or "").strip().lower()
        tid = name2id.get(cn)
        if tid:
            tally["transporter_linked"] += 1
        prepared.append(dict(rec, transporter_id=tid))
    for i in range(0, len(prepared), DRIVER_BATCH):
        async with engine.begin() as conn:
            for rec in prepared[i:i + DRIVER_BATCH]:
                res = await conn.execute(stmt, rec)
                row = res.mappings().first()
                if row is None:
                    tally["skipped"] += 1
                elif row.get("inserted"):
                    tally["inserted"] += 1
                else:
                    tally["updated"] += 1
    return tally


async def import_pdp(rows: List[Dict[str, Any]], dsn: str) -> int:
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    engine = get_engine(dsn)
    recs = [r for r in (clean_pdp(x) for x in rows) if r]
    stmt = text(_PDP_UPSERT)
    n = 0
    for i in range(0, len(recs), PDP_BATCH):
        batch = recs[i:i + PDP_BATCH]
        async with engine.begin() as conn:
            await conn.execute(stmt, batch)
        n += len(batch)
    return n


# --- report ------------------------------------------------------------------
def build_driver_report(raw_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid, invalid = [], []
    issues, names, lic = Counter(), Counter(), Counter()
    for raw in raw_rows:
        rec, iss = clean_driver(raw)
        for x in iss:
            issues[x] += 1
        if rec is None:
            invalid.append({"srno": raw.get("Srno"), "licence": raw.get("licence_number"),
                            "reasons": iss})
        else:
            valid.append(rec)
            names[rec["name"].lower()] += 1
            lic[rec["licence_no_norm"]] += 1
    dup_lic = {k: v for k, v in lic.items() if v > 1}
    return {"total": len(raw_rows), "valid": valid, "invalid": invalid,
            "issues": dict(issues),
            "distinct_licences": len(lic),
            "dup_name_groups": sum(1 for v in names.values() if v > 1),
            "dup_licence_groups": len(dup_lic)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-pdp", action="store_true")
    args = ap.parse_args()
    if not Path(args.xlsx).exists():
        print(f"FATAL: xlsx not found: {args.xlsx}", file=sys.stderr)
        return 2

    drivers_raw = load_sheet(args.xlsx, "Application Data", _APP_COLS, args.limit)
    rep = build_driver_report(drivers_raw)

    dtally = None
    ptotal = None
    if not args.dry_run:
        async def run():
            name2id = await resolve_transporters(args.dsn)
            dt_ = await import_drivers(rep["valid"], name2id, args.dsn)
            pt_ = None
            if not args.skip_pdp:
                pdp_raw = load_sheet(args.xlsx, "PDP Data",
                                     ("pdp_id", "acceptance_time_stamp", "active", "appl_number",
                                      "pdp_number", "validity", "remarks", "pdp_cancelled_by",
                                      "cancellation_time"), args.limit)
                pt_ = await import_pdp(pdp_raw, args.dsn)
            return dt_, pt_
        dtally, ptotal = asyncio.run(run())

    print("\n" + "=" * 66)
    print("DRIVER MASTER IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 66)
    print(f"  Application Data rows : {rep['total']}")
    print(f"  importable (valid)    : {len(rep['valid'])}")
    print(f"  invalid (not imported): {len(rep['invalid'])}")
    print("\n  IMPORT SUMMARY (driver_master)")
    if args.dry_run:
        print(f"    inserted (projected): {rep['distinct_licences']} (distinct licences; "
              f"{len(rep['valid']) - rep['distinct_licences']} collapsed by upsert)")
        print("    updated / skipped   : n/a (dry-run)")
    else:
        print(f"    inserted            : {dtally['inserted']}")
        print(f"    updated             : {dtally['updated']}")
        print(f"    skipped (no change) : {dtally['skipped']}")
        print(f"    transporter-linked  : {dtally['transporter_linked']}")
        if ptotal is not None:
            print(f"  driver_pdp_history upserted: {ptotal}")
    print(f"    invalid             : {len(rep['invalid'])}")
    print("\n  VALIDATION / CLEANING")
    for k in sorted(rep["issues"]):
        print(f"    {k:20}: {rep['issues'][k]}")
    print(f"    dup_licence_groups  : {rep['dup_licence_groups']}")
    print(f"    dup_name_groups     : {rep['dup_name_groups']} (distinct people)")
    if rep["invalid"]:
        print("\n  INVALID SAMPLES:")
        for r in rep["invalid"][:10]:
            print(f"    {r}")
    print("=" * 66 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
