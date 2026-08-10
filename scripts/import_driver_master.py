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
    POSTGRES_DSN='postgresql+asyncpg://postgres:$RDS_PW@__RDS_HOST__:5432/jnpa_schema_v3?ssl=require' \
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
# Application database = AWS RDS (jnpa_schema_v3). No local-postgres fallback:
# set POSTGRES_DSN (or pass --dsn) or the script refuses to run.
DEFAULT_DSN = os.environ.get("POSTGRES_DSN", "")
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
    srno = clean_int(raw.get("Srno"))
    if not licence_norm:
        return None, ["missing_licence"]
    if not name:
        return None, ["missing_name"]
    # Srno is the stable natural key (driver_id / id / source_srno). Without it
    # a row cannot be addressed idempotently, so it is a hard reject.
    if srno is None:
        return None, ["missing_srno"]
    valid_to, bad_valid = clean_date(raw.get("validity"), min_year=2000, max_year=2100)
    if bad_valid:
        issues.append("validity_invalid")
    dob, bad_dob = clean_date(raw.get("dob(YYYY-MM-DD)"))
    if bad_dob:
        issues.append("dob_invalid")
    rec = {
        "driver_id": srno,
        "licence_no": licence_no,
        "licence_no_norm": licence_norm,
        "source_srno": srno,
        "name": name,
        "company_name": clean_text(raw.get("company_name")),
        "photo_file": clean_text(raw.get("photo")),
        "licence_type": clean_text(raw.get("Licence type")) or "HMV",
        "licence_valid_to": valid_to,
        "latest_pdp_number": clean_text(raw.get("latest_pdp_number")),
        "dob": dob,
    }
    return rec, issues


# v3 runtime schema (core.driver). Three things the previous version got wrong,
# each of which made this statement unrunnable or lossy against jnpa_qa:
#
#   * driver_id is the PK and is GENERATED ALWAYS AS IDENTITY, so pinning it to
#     the sheet's Srno requires OVERRIDING SYSTEM VALUE — without that clause
#     Postgres rejects the insert ("cannot insert a non-DEFAULT value into column
#     driver_id"). (information_schema.column_default is empty for identity
#     columns, which is what made this look like a plain NOT NULL column.)
#   * licence_no_norm is a PLAIN column (is_generated = NEVER), not GENERATED, so
#     omitting it left it NULL and broke search, /stats and the PDP join.
#   * the old arbiter `ON CONFLICT (licence_no_norm) WHERE id < 100000000` names
#     an index that does not exist in v3 — Postgres rejects the statement at plan
#     time with "no unique or exclusion constraint matching the ON CONFLICT
#     specification". It also COLLAPSED the 348 legitimate duplicate-licence
#     groups, yielding 31,498 rows instead of the source's 31,846.
#
# Keying on driver_id (the real PK) preserves every source row and is idempotent.
_DRIVER_UPSERT = """
INSERT INTO core.driver AS d
    (driver_id, id, licence_number, licence_no_norm, source_srno, driver_name,
     company_name, transporter_id, photo_file, licence_type, licence_valid_to,
     latest_pdp_number, date_of_birth)
OVERRIDING SYSTEM VALUE
VALUES
    (:driver_id, :driver_id, :licence_no, :licence_no_norm, :source_srno, :name,
     :company_name, :transporter_id, :photo_file, :licence_type, :licence_valid_to,
     :latest_pdp_number, :dob)
ON CONFLICT (driver_id) DO UPDATE SET
    licence_number = EXCLUDED.licence_number,
    licence_no_norm = EXCLUDED.licence_no_norm, source_srno = EXCLUDED.source_srno,
    driver_name = EXCLUDED.driver_name, company_name = EXCLUDED.company_name,
    transporter_id = EXCLUDED.transporter_id, photo_file = EXCLUDED.photo_file,
    licence_type = EXCLUDED.licence_type, licence_valid_to = EXCLUDED.licence_valid_to,
    latest_pdp_number = EXCLUDED.latest_pdp_number, date_of_birth = EXCLUDED.date_of_birth,
    updated_at = now()
WHERE (d.driver_name, d.company_name, d.transporter_id, d.photo_file, d.licence_type,
       d.licence_valid_to, d.latest_pdp_number, d.date_of_birth, d.source_srno,
       d.licence_number, d.licence_no_norm)
  IS DISTINCT FROM
      (EXCLUDED.driver_name, EXCLUDED.company_name, EXCLUDED.transporter_id, EXCLUDED.photo_file,
       EXCLUDED.licence_type, EXCLUDED.licence_valid_to, EXCLUDED.latest_pdp_number,
       EXCLUDED.date_of_birth, EXCLUDED.source_srno, EXCLUDED.licence_number,
       EXCLUDED.licence_no_norm)
RETURNING (xmax = 0) AS inserted
"""


# --- PDP history rows --------------------------------------------------------
def clean_pdp(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pdp_id = clean_int(raw.get("pdp_id"))
    if pdp_id is None:
        return None
    validity, _ = clean_date(raw.get("validity"), min_year=2000, max_year=2100)
    cancellation_time = raw.get("cancellation_time")
    cancellation_date = None
    if isinstance(cancellation_time, dt.datetime):
        cancellation_date = cancellation_time.date()
    elif isinstance(cancellation_time, dt.date):
        cancellation_date = cancellation_time
    return {
        "pdp_id": pdp_id,
        "acceptance_time_stamp": raw.get("acceptance_time_stamp"),
        "active": bool(raw.get("active")),
        "appl_number": clean_text(raw.get("appl_number")),
        "pdp_number": clean_text(raw.get("pdp_number")),
        "validity": validity,
        "remarks": clean_text(raw.get("remarks")),
        "pdp_cancelled_by": clean_text(raw.get("pdp_cancelled_by")),
        "cancellation_time": cancellation_time,
        "cancellation_date": cancellation_date,
    }


# v3 runtime schema (core.pdp): arch column names accepted_at / valid_until /
# cancelled_by / cancellation_date, plus the 0102 ext column cancellation_time.
_PDP_UPSERT = """
INSERT INTO core.pdp
    (pdp_id, accepted_at, active, appl_number, pdp_number, valid_until,
     remarks, cancelled_by, cancellation_date, cancellation_time)
VALUES
    (:pdp_id, :acceptance_time_stamp, :active, :appl_number, :pdp_number, :validity,
     :remarks, :pdp_cancelled_by, :cancellation_date, :cancellation_time)
ON CONFLICT (pdp_id) DO UPDATE SET
    accepted_at = EXCLUDED.accepted_at, active = EXCLUDED.active,
    appl_number = EXCLUDED.appl_number, pdp_number = EXCLUDED.pdp_number,
    valid_until = EXCLUDED.valid_until, remarks = EXCLUDED.remarks,
    cancelled_by = EXCLUDED.cancelled_by, cancellation_date = EXCLUDED.cancellation_date,
    cancellation_time = EXCLUDED.cancellation_time
"""


# --- data quality ------------------------------------------------------------
# core.dq_issue traps: issue_id is GENERATED ALWAYS AS IDENTITY (so a
# content-derived id needs OVERRIDING SYSTEM VALUE), and severity is
# CHECK-constrained to lowercase info|warn|error. file_id is an FK to
# core.ingest_file, so it stays NULL for a script-driven import.
DQ_SEVERITY = {"DUPLICATE_LICENCE": "warn", "DOB_INVALID": "warn",
               "VALIDITY_INVALID": "warn", "PDP_NUMBER_COLLISION": "info",
               "CANCELLATION_DATE_MISSING": "warn",
               "TRANSPORTER_UNRESOLVED": "info", "ROW_REJECTED": "error"}


def dq_id(issue_type: str, record_ref: str) -> int:
    """Deterministic 62-bit id from (issue_type, record_ref).

    core.dq_issue has no sequence, and re-running the import must not pile up
    duplicate findings. A content-derived id lets the insert be an idempotent
    ON CONFLICT (issue_id) upsert instead of a delete-then-reinsert, which would
    mean issuing DELETEs against a live QA database.
    """
    import hashlib

    h = hashlib.blake2b(f"{issue_type}|{record_ref}".encode("utf-8"), digest_size=8)
    return int.from_bytes(h.digest(), "big") & 0x3FFFFFFFFFFFFFFF


_DQ_UPSERT = """
INSERT INTO core.dq_issue
    (issue_id, file_id, source_table, record_ref, issue_type, severity, description)
OVERRIDING SYSTEM VALUE
VALUES (:issue_id, NULL, :source_table, :record_ref, :issue_type, :severity, :description)
ON CONFLICT (issue_id) DO UPDATE SET
    description = EXCLUDED.description, detected_at = now()
"""


def dq_row(issue_type: str, source_table: str, record_ref: str, description: str) -> Dict[str, Any]:
    return {"issue_id": dq_id(issue_type, record_ref), "source_table": source_table,
            "record_ref": record_ref, "issue_type": issue_type,
            "severity": DQ_SEVERITY.get(issue_type, "warn"),
            "description": description}


def collect_dq(rep: Dict[str, Any], pdp_rows: List[Dict[str, Any]],
               unresolved: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every anomaly the import tolerated, as core.dq_issue rows.

    Nothing here changes what is imported — source rows are preserved intact and
    the finding is recorded alongside them.
    """
    out: List[Dict[str, Any]] = []

    # 348 duplicate-licence groups: BOTH rows are kept; the group is flagged.
    for norm, srnos in sorted(rep["dup_licence_map"].items()):
        out.append(dq_row(
            "DUPLICATE_LICENCE", "core.driver", f"licence_no_norm={norm}",
            f"{len(srnos)} driver rows share licence {norm} (Srno {sorted(srnos)}); "
            "all rows retained"))

    for srno, field in rep["field_issues"]:
        if field == "dob_invalid":
            out.append(dq_row("DOB_INVALID", "core.driver", f"driver_id={srno}",
                              f"Srno {srno}: date_of_birth outside the accepted range; stored NULL"))
        elif field == "validity_invalid":
            out.append(dq_row("VALIDITY_INVALID", "core.driver", f"driver_id={srno}",
                              f"Srno {srno}: licence validity unparseable; stored NULL"))

    for bad in rep["invalid"]:
        out.append(dq_row("ROW_REJECTED", "core.driver", f"srno={bad.get('srno')}",
                          f"row not imported: {', '.join(bad.get('reasons') or [])}"))

    for rec in unresolved:
        out.append(dq_row(
            "TRANSPORTER_UNRESOLVED", "core.driver", f"driver_id={rec['driver_id']}",
            f"company_name {rec.get('company_name')!r} did not match core.transporter; "
            "transporter_id left NULL"))

    # PDP-number collisions: distinct permits legitimately sharing a pdp_number.
    # Readers already disambiguate by accepted_at; every row is retained.
    by_number: Dict[str, List[int]] = {}
    for r in pdp_rows:
        num = r.get("pdp_number")
        if num:
            by_number.setdefault(num, []).append(r["pdp_id"])
    for num, ids in sorted(by_number.items()):
        if len(ids) > 1:
            out.append(dq_row(
                "PDP_NUMBER_COLLISION", "core.pdp", f"pdp_number={num}",
                f"{len(ids)} permits share pdp_number {num} (pdp_id {sorted(ids)}); "
                "all rows retained, latest resolved by accepted_at"))
        # A cancelled permit with no cancellation date is an audit gap worth flagging.
    for r in pdp_rows:
        if r.get("pdp_cancelled_by") and not r.get("cancellation_date"):
            out.append(dq_row(
                "CANCELLATION_DATE_MISSING", "core.pdp", f"pdp_id={r['pdp_id']}",
                f"cancelled by {r['pdp_cancelled_by']!r} but no cancellation date supplied"))
    return out


async def import_dq(rows: List[Dict[str, Any]], dsn: str) -> int:
    if not rows:
        return 0
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    engine = get_engine(dsn)
    stmt = text(_DQ_UPSERT)
    # De-duplicate by issue_id so a single batch never hits "ON CONFLICT DO UPDATE
    # command cannot affect row a second time".
    unique = list({r["issue_id"]: r for r in rows}.values())
    for i in range(0, len(unique), PDP_BATCH):
        async with engine.begin() as conn:
            await conn.execute(stmt, unique[i:i + PDP_BATCH])
    return len(unique)


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

    rows = await fetch_all("SELECT id, company_name AS name FROM core.transporter", {}, dsn=dsn)
    return {str(r["name"]).strip().lower(): int(r["id"]) for r in rows if r.get("name")}


def link_transporters(
    records: List[Dict[str, Any]], name2id: Dict[str, int]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Attach transporter_id by exact normalised company name.

    Deterministic only — an exact case/whitespace-insensitive match or nothing.
    No fuzzy matching, so an unresolved company leaves transporter_id NULL and
    is reported rather than guessed at. Pure, so the dry-run reports the same
    numbers the live import will produce.
    """
    prepared: List[Dict[str, Any]] = []
    unresolved: List[Dict[str, Any]] = []
    for rec in records:
        cn = (rec.get("company_name") or "").strip().lower()
        tid = name2id.get(cn) if cn else None
        prepared.append(dict(rec, transporter_id=tid))
        if tid is None:
            unresolved.append(rec)
    return prepared, unresolved


async def import_drivers(prepared: List[Dict[str, Any]], dsn: str) -> Dict[str, int]:
    """Chunked upsert: one committed transaction per DRIVER_BATCH rows (far fewer
    round-trips than a transaction per row), keeping per-row RETURNING granularity.

    ``prepared`` rows already carry transporter_id (see ``link_transporters``).
    """
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    engine = get_engine(dsn)
    stmt = text(_DRIVER_UPSERT)
    tally = {"inserted": 0, "updated": 0, "skipped": 0}
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
    # OVERRIDING SYSTEM VALUE writes driver_id 1..N without advancing the
    # identity sequence behind it, and the explicit id does the same to
    # core.driver_id_seq. The upload path lets both generate values, so push each
    # sequence past the imported range or the next UI upload collides on the PK.
    async with engine.begin() as conn:
        await conn.execute(text(
            "SELECT setval(pg_get_serial_sequence('core.driver', 'driver_id'), "
            "GREATEST((SELECT COALESCE(max(driver_id), 1) FROM core.driver), 1))"))
        await conn.execute(text(
            "SELECT setval('core.driver_id_seq', "
            "GREATEST((SELECT COALESCE(max(id), 1) FROM core.driver), 1))"))
    return tally


async def import_pdp(recs: List[Dict[str, Any]], dsn: str) -> int:
    """Upsert already-cleaned PDP records (see ``clean_pdp``).

    Unchanged from the working version apart from taking cleaned rows: the
    ON CONFLICT (pdp_id) arbiter is the real primary key and plans fine on v3.
    """
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    engine = get_engine(dsn)
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
    field_issues: List[Tuple[Any, str]] = []
    issues, names = Counter(), Counter()
    lic_srnos: Dict[str, List[int]] = {}
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
            lic_srnos.setdefault(rec["licence_no_norm"], []).append(rec["driver_id"])
            for x in iss:
                field_issues.append((rec["driver_id"], x))
    dup_lic = {k: v for k, v in lic_srnos.items() if len(v) > 1}
    return {"total": len(raw_rows), "valid": valid, "invalid": invalid,
            "issues": dict(issues),
            "distinct_licences": len(lic_srnos),
            "dup_name_groups": sum(1 for v in names.values() if v > 1),
            "dup_licence_groups": len(dup_lic),
            "dup_licence_map": dup_lic,
            "field_issues": field_issues}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=DEFAULT_XLSX)
    ap.add_argument(
        "--dsn",
        default=DEFAULT_DSN,
        required=not DEFAULT_DSN,
        help="SQLAlchemy asyncpg DSN for the RDS database "
             "(defaults to $POSTGRES_DSN; no local fallback)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--skip-pdp", action="store_true")
    ap.add_argument("--skip-drivers", action="store_true",
                    help="import PDP + DQ only — for resuming after the driver "
                         "phase has already landed (the upsert is idempotent, so "
                         "this only saves time, never correctness)")
    args = ap.parse_args()
    if not Path(args.xlsx).exists():
        print(f"FATAL: xlsx not found: {args.xlsx}", file=sys.stderr)
        return 2

    drivers_raw = load_sheet(args.xlsx, "Application Data", _APP_COLS, args.limit)
    rep = build_driver_report(drivers_raw)

    pdp_raw = [] if args.skip_pdp else load_sheet(
        args.xlsx, "PDP Data",
        ("pdp_id", "acceptance_time_stamp", "active", "appl_number", "pdp_number",
         "validity", "remarks", "pdp_cancelled_by", "cancellation_time"), args.limit)
    pdp_clean = [r for r in (clean_pdp(x) for x in pdp_raw) if r]

    dtally = None
    ptotal = None
    dq_written = None
    unresolved: List[Dict[str, Any]] = []
    dq_rows: List[Dict[str, Any]] = []
    if args.dry_run:
        # No DB, so no transporter table to match against: every row counts as
        # unresolved for the projection and the real number is reported live.
        dq_rows = collect_dq(rep, pdp_clean, [])
    else:
        async def run():
            name2id = await resolve_transporters(args.dsn)
            prepared, unres = link_transporters(rep["valid"], name2id)
            if args.skip_drivers:
                dt_ = {"inserted": 0, "updated": 0, "skipped": len(prepared)}
            else:
                dt_ = await import_drivers(prepared, args.dsn)
            dt_["transporter_linked"] = len(prepared) - len(unres)
            dt_["transporter_unresolved"] = len(unres)
            pt_ = None if args.skip_pdp else await import_pdp(pdp_clean, args.dsn)
            rows = collect_dq(rep, pdp_clean, unres)
            dq_n = await import_dq(rows, args.dsn)
            return dt_, pt_, unres, rows, dq_n
        dtally, ptotal, unresolved, dq_rows, dq_written = asyncio.run(run())

    print("\n" + "=" * 66)
    print("DRIVER MASTER IMPORT" + ("  [DRY-RUN — no DB writes]" if args.dry_run else ""))
    print("=" * 66)
    print(f"  Application Data rows : {rep['total']}")
    print(f"  importable (valid)    : {len(rep['valid'])}")
    print(f"  invalid (not imported): {len(rep['invalid'])}")
    print(f"  PDP Data rows         : {len(pdp_raw)}  (clean: {len(pdp_clean)})")
    print("\n  IMPORT SUMMARY (driver_master)")
    if args.dry_run:
        # Keyed on driver_id (Srno), so EVERY valid row lands — duplicate
        # licences are retained, not collapsed.
        print(f"    rows to upsert (projected): {len(rep['valid'])} "
              f"(keyed on driver_id=Srno; {rep['dup_licence_groups']} duplicate-licence "
              "groups retained)")
        print(f"    pdp rows to upsert        : {len(pdp_clean)}")
        print("    transporter linkage       : n/a (dry-run, no DB lookup)")
    else:
        print(f"    inserted            : {dtally['inserted']}")
        print(f"    updated             : {dtally['updated']}")
        print(f"    skipped (no change) : {dtally['skipped']}")
        print(f"    transporter-linked  : {dtally['transporter_linked']}")
        print(f"    transporter-unresolved: {dtally['transporter_unresolved']}")
        if ptotal is not None:
            print(f"    pdp rows upserted   : {ptotal}")
        print(f"    dq issues written   : {dq_written}")
    print(f"    invalid             : {len(rep['invalid'])}")
    print("\n  VALIDATION / CLEANING")
    for k in sorted(rep["issues"]):
        print(f"    {k:20}: {rep['issues'][k]}")
    print(f"    dup_licence_groups  : {rep['dup_licence_groups']}")
    print(f"    dup_name_groups     : {rep['dup_name_groups']} (distinct people)")
    print("\n  DATA QUALITY (core.dq_issue)")
    dq_by_type = Counter(r["issue_type"] for r in dq_rows)
    for k in sorted(dq_by_type):
        print(f"    {k:26}: {dq_by_type[k]}")
    print(f"    {'TOTAL':26}: {len(dq_rows)}")
    if rep["invalid"]:
        print("\n  INVALID SAMPLES:")
        for r in rep["invalid"][:10]:
            print(f"    {r}")
    print("=" * 66 + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
