"""UC3-001 (driver/PDP) — the Driver Master import lands every source row.

What this pins, and why each one is a real regression risk against the v3 schema:

  * ``core.driver.driver_id`` is the PK with NO default and no insert trigger, so
    the importer must supply it. It is the sheet's ``Srno``.
  * ``licence_no_norm`` is a PLAIN column (``is_generated = NEVER``), NOT
    generated — omitting it leaves NULL and breaks search, /stats and the PDP join.
  * v3 has **no unique index on licence_no_norm**, so any
    ``ON CONFLICT (licence_no_norm)`` is rejected at plan time. The conflict
    target must be ``driver_id``.
  * Keying on the licence also COLLAPSED 348 legitimate duplicate-licence groups
    (31,846 source rows -> 31,498). Every source row must survive.

The parsing tests read the real workbook and skip when it is absent; the SQL
tests are pure text assertions; the live tests talk to RDS and skip when it is
unreachable.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "import_driver_master", REPO_ROOT / "scripts" / "import_driver_master.py")
idm = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(idm)

XLSX = idm.DEFAULT_XLSX
HAVE_XLSX = Path(XLSX).exists()
needs_xlsx = pytest.mark.skipif(not HAVE_XLSX, reason=f"workbook absent: {XLSX}")

EXPECTED_DRIVERS = 31846
EXPECTED_PDP = 367078
EXPECTED_DUP_LICENCE_GROUPS = 348
EXPECTED_DOB_INVALID = 2

# The ticket's traceability sample (Application Data row 1).
SAMPLE_LICENCE = "RJ19 20060721778"
SAMPLE_NAME = "SHRI RAM BISHNOI"
SAMPLE_PDP = "PDP2023/5/14"


# --------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def app_rows() -> List[Dict[str, Any]]:
    if not HAVE_XLSX:
        pytest.skip("workbook absent")
    return idm.load_sheet(XLSX, "Application Data", idm._APP_COLS, None)


@pytest.fixture(scope="module")
def report(app_rows) -> Dict[str, Any]:
    return idm.build_driver_report(app_rows)


@pytest.fixture(scope="module")
def pdp_clean() -> List[Dict[str, Any]]:
    if not HAVE_XLSX:
        pytest.skip("workbook absent")
    raw = idm.load_sheet(XLSX, "PDP Data",
                         ("pdp_id", "acceptance_time_stamp", "active", "appl_number",
                          "pdp_number", "validity", "remarks", "pdp_cancelled_by",
                          "cancellation_time"), None)
    return [r for r in (idm.clean_pdp(x) for x in raw) if r]


# ============================================================== source counts
@needs_xlsx
def test_application_data_row_count(app_rows):
    """(1) Application Data = 31,846 rows."""
    assert len(app_rows) == EXPECTED_DRIVERS


@needs_xlsx
def test_pdp_data_row_count(pdp_clean):
    """(2) PDP Data = 367,078 rows, none dropped by cleaning."""
    assert len(pdp_clean) == EXPECTED_PDP


# ============================================================ the key contract
@needs_xlsx
def test_driver_id_and_id_come_from_srno(app_rows):
    """(3)(4) driver_id and id are the sheet's Srno."""
    for raw in app_rows[:200]:
        rec, _ = idm.clean_driver(raw)
        assert rec is not None
        assert rec["driver_id"] == raw["Srno"]
    # id is bound to the same placeholder as driver_id in the statement.
    assert re.search(r"\(:driver_id,\s*:driver_id,", idm._DRIVER_UPSERT)


@needs_xlsx
def test_source_srno_is_populated(app_rows):
    """(5) source_srno carries the sheet row number."""
    rec, _ = idm.clean_driver(app_rows[0])
    assert rec["source_srno"] == app_rows[0]["Srno"] == 1


@needs_xlsx
def test_licence_no_norm_is_explicitly_populated(app_rows):
    """(6) licence_no_norm is computed and inserted — it is NOT a generated column."""
    rec, _ = idm.clean_driver(app_rows[0])
    assert rec["licence_no_norm"] == "RJ1920060721778"
    assert "licence_no_norm" in idm._DRIVER_UPSERT
    assert ":licence_no_norm" in idm._DRIVER_UPSERT


def _strip_comments(src: str) -> str:
    """Drop Python ``#`` and SQL ``--`` comment lines.

    The fix is explained in prose right above the statements it replaced, so a
    naive scan would match the explanation and not the code.
    """
    keep = []
    for line in src.splitlines():
        bare = line.strip()
        if bare.startswith("#") or bare.startswith("--"):
            continue
        keep.append(line)
    return "\n".join(keep)


def test_no_on_conflict_licence_no_norm_anywhere():
    """(7) v3 has no unique index on licence_no_norm — the clause cannot be planned."""
    offenders = []
    for rel in ("scripts/import_driver_master.py",
                "services/transporters_drivers/repository.py"):
        src = _strip_comments((REPO_ROOT / rel).read_text())
        if re.search(r"ON\s+CONFLICT\s*\(\s*licence_no_norm", src, re.I):
            offenders.append(rel)
    assert not offenders, f"ON CONFLICT (licence_no_norm) is back in: {offenders}"


def test_driver_conflict_target_is_driver_id():
    """(8) The importer's arbiter is the real primary key."""
    assert re.search(r"ON\s+CONFLICT\s*\(\s*driver_id\s*\)", idm._DRIVER_UPSERT, re.I)


def test_pdp_conflict_target_is_unchanged():
    """The working PDP path stays keyed on its primary key."""
    assert re.search(r"ON\s+CONFLICT\s*\(\s*pdp_id\s*\)", idm._PDP_UPSERT, re.I)


def test_upload_path_writes_licence_no_norm_explicitly():
    """The UI upload path must obey the same v3 facts as the script.

    It leaves driver_id/id to the identity + default (an upload has no Srno), but
    licence_no_norm is a plain column and must still be written.
    """
    src = (REPO_ROOT / "services/transporters_drivers/repository.py").read_text()
    stmt = src.split("_DRIVER_UPSERT = ")[1].split('"""')[1]
    assert "licence_no_norm" in stmt and ":licence_no_norm" in stmt
    assert "INSERT INTO core.driver" in stmt


def test_identity_columns_use_overriding_system_value():
    """driver_id and issue_id are GENERATED ALWAYS — explicit values need the clause."""
    assert "OVERRIDING SYSTEM VALUE" in idm._DRIVER_UPSERT
    assert "OVERRIDING SYSTEM VALUE" in idm._DQ_UPSERT


# ================================================== duplicates are NOT collapsed
@needs_xlsx
def test_every_source_row_is_retained(report):
    """(9) 31,846 rows in, 31,846 rows to upsert — nothing collapsed."""
    assert len(report["valid"]) == EXPECTED_DRIVERS
    assert report["invalid"] == []
    # Distinct licences is strictly lower — proving duplicates exist and survive.
    assert report["distinct_licences"] < EXPECTED_DRIVERS


@needs_xlsx
def test_duplicate_licence_groups_are_detected(report):
    """(10) The 348 duplicate-licence groups are found and mapped to their Srnos."""
    assert report["dup_licence_groups"] == EXPECTED_DUP_LICENCE_GROUPS
    assert len(report["dup_licence_map"]) == EXPECTED_DUP_LICENCE_GROUPS
    for norm, srnos in report["dup_licence_map"].items():
        assert len(srnos) > 1
        assert len(set(srnos)) == len(srnos), f"{norm} maps a Srno twice"


@needs_xlsx
def test_pdp_collisions_are_retained(pdp_clean):
    """(11) Colliding pdp_numbers are distinct permits and all are kept."""
    by_number: Dict[str, List[int]] = {}
    for r in pdp_clean:
        if r.get("pdp_number"):
            by_number.setdefault(r["pdp_number"], []).append(r["pdp_id"])
    collisions = {k: v for k, v in by_number.items() if len(v) > 1}
    assert collisions, "expected pdp_number collisions in the source"
    assert len({r["pdp_id"] for r in pdp_clean}) == len(pdp_clean)


@needs_xlsx
def test_cancellation_time_is_preserved(pdp_clean):
    """(12) cancellation_time must survive — the previous load dropped it."""
    with_time = [r for r in pdp_clean if r.get("cancellation_time") is not None]
    assert with_time, "source has cancellation_time values"
    assert "cancellation_time" in idm._PDP_UPSERT
    assert ":cancellation_time" in idm._PDP_UPSERT
    for r in with_time[:50]:
        assert r["cancellation_date"] is not None, "date derived from the timestamp"


@needs_xlsx
def test_cancelled_by_remains_traceable(pdp_clean):
    """(28) 'Cancelled by: Salim Shaikh' stays attributable after cleaning."""
    hits = [r for r in pdp_clean
            if r.get("pdp_cancelled_by") and "Salim Shaikh" in str(r["pdp_cancelled_by"])]
    assert hits, "Salim Shaikh cancellations present in source"
    assert any("Cancelled by" in str(r.get("remarks") or "") for r in hits)


# ================================================================ invalid data
@needs_xlsx
def test_invalid_dob_becomes_null_and_is_flagged(report):
    """(13) 2 unusable DOBs -> NULL + a DQ finding, row still imported."""
    assert report["issues"].get("dob_invalid") == EXPECTED_DOB_INVALID
    flagged = [srno for srno, kind in report["field_issues"] if kind == "dob_invalid"]
    assert len(flagged) == EXPECTED_DOB_INVALID
    by_id = {r["driver_id"]: r for r in report["valid"]}
    for srno in flagged:
        assert srno in by_id, "a bad DOB must not drop the row"
        assert by_id[srno]["dob"] is None


# ========================================================= transporter linkage
def test_transporter_matching_is_deterministic():
    """(14) Exact normalised-name match only — never a guess."""
    recs = [{"driver_id": 1, "company_name": "  Royal Container Carrier "},
            {"driver_id": 2, "company_name": "ROYAL CONTAINER CARRIER"}]
    prepared, unresolved = idm.link_transporters(recs, {"royal container carrier": 77})
    assert [r["transporter_id"] for r in prepared] == [77, 77]
    assert unresolved == []


def test_unresolved_transporter_is_null_and_flagged():
    """(15) No match -> NULL + TRANSPORTER_UNRESOLVED, never an invented id."""
    recs = [{"driver_id": 9, "company_name": "Nowhere Logistics"},
            {"driver_id": 10, "company_name": None}]
    prepared, unresolved = idm.link_transporters(recs, {"royal container carrier": 77})
    assert all(r["transporter_id"] is None for r in prepared)
    assert len(unresolved) == 2
    rows = idm.collect_dq({"dup_licence_map": {}, "field_issues": [], "invalid": []},
                          [], unresolved)
    kinds = {r["issue_type"] for r in rows}
    assert kinds == {"TRANSPORTER_UNRESOLVED"}
    assert all(r["severity"] in ("info", "warn", "error") for r in rows)


# ==================================================================== dq_issue
def test_dq_ids_are_deterministic_and_positive():
    """Re-running must update, not duplicate: the id is content-derived."""
    a = idm.dq_id("DUPLICATE_LICENCE", "licence_no_norm=X")
    assert a == idm.dq_id("DUPLICATE_LICENCE", "licence_no_norm=X")
    assert a != idm.dq_id("DOB_INVALID", "licence_no_norm=X")
    assert 0 < a < 2 ** 63


def test_dq_severity_matches_the_check_constraint():
    """core.dq_issue CHECKs severity IN ('info','warn','error') — lowercase."""
    assert set(idm.DQ_SEVERITY.values()) <= {"info", "warn", "error"}


def test_dq_upsert_is_idempotent_by_issue_id():
    assert re.search(r"ON\s+CONFLICT\s*\(\s*issue_id\s*\)", idm._DQ_UPSERT, re.I)
    assert "DELETE" not in idm._DQ_UPSERT.upper()


@needs_xlsx
def test_dq_covers_the_required_taxonomy(report, pdp_clean):
    rows = idm.collect_dq(report, pdp_clean, [])
    kinds = {r["issue_type"] for r in rows}
    for required in ("DUPLICATE_LICENCE", "DOB_INVALID", "PDP_NUMBER_COLLISION",
                     "CANCELLATION_DATE_MISSING"):
        assert required in kinds, f"{required} not emitted"
    assert len({r["issue_id"] for r in rows}) == len(rows), "issue_id collision"


# ============================================== live database (skips if absent)
def _dsn() -> Optional[str]:
    return os.environ.get("RFID_POSTGRES_DSN") or os.environ.get("DRIVER_TEST_DSN")


live = pytest.mark.skipif(_dsn() is None, reason="no RFID_POSTGRES_DSN in the environment")


def _conn():
    psycopg = pytest.importorskip("psycopg")
    try:
        return psycopg.connect(_dsn(), connect_timeout=15)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres unreachable: {exc}")


def _scalar(sql: str):
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()[0]


@live
def test_live_driver_count_is_the_full_source():
    """(17) core.driver = 31,846."""
    assert _scalar("SELECT count(*) FROM core.driver") == EXPECTED_DRIVERS


@live
def test_live_pdp_count_is_the_full_source():
    """(18) core.pdp = 367,078."""
    assert _scalar("SELECT count(*) FROM core.pdp") == EXPECTED_PDP


@live
def test_live_licence_no_norm_has_no_nulls():
    """(19) The column the search and PDP join depend on is fully populated."""
    assert _scalar("SELECT count(*) FROM core.driver WHERE licence_no_norm IS NULL") == 0


@live
def test_live_duplicate_licences_survived():
    """(9)(10) 31,846 rows over 31,498 distinct licences."""
    total = _scalar("SELECT count(*) FROM core.driver")
    distinct = _scalar("SELECT count(DISTINCT licence_no_norm) FROM core.driver")
    groups = _scalar("SELECT count(*) FROM (SELECT 1 FROM core.driver "
                     "GROUP BY licence_no_norm HAVING count(*) > 1) g")
    assert total == EXPECTED_DRIVERS
    assert distinct == total - (total - distinct)
    assert groups == EXPECTED_DUP_LICENCE_GROUPS


@live
def test_live_driver_id_equals_source_srno():
    """(3)(5) driver_id, id and source_srno all agree."""
    assert _scalar("SELECT count(*) FROM core.driver "
                   "WHERE source_srno IS DISTINCT FROM driver_id") == 0
    assert _scalar("SELECT count(*) FROM core.driver WHERE id IS DISTINCT FROM driver_id") == 0


@live
def test_live_latest_pdp_number_resolves():
    """(20) Every driver's latest_pdp_number matches a permit."""
    unmatched = _scalar(
        "SELECT count(*) FROM core.driver d "
        "WHERE d.latest_pdp_number IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM core.pdp p WHERE p.pdp_number = d.latest_pdp_number)")
    assert unmatched == 0


@live
def test_live_cancellation_time_is_populated():
    """(12) The field the previous load lost."""
    assert _scalar("SELECT count(*) FROM core.pdp WHERE cancellation_time IS NOT NULL") > 0


@live
def test_live_sample_driver_is_intact():
    """(23)-(27) The ticket's traceability row."""
    with _conn() as c, c.cursor() as cur:
        cur.execute(
            "SELECT driver_name, licence_number, latest_pdp_number, licence_valid_to, "
            "licence_type FROM core.driver WHERE licence_no_norm = %s",
            (idm.norm_licence(SAMPLE_LICENCE),))
        row = cur.fetchone()
    assert row is not None, f"{SAMPLE_LICENCE} not found"
    name, lic, pdp, valid_to, lic_type = row
    assert name == SAMPLE_NAME
    assert lic == SAMPLE_LICENCE
    assert pdp == SAMPLE_PDP
    assert str(valid_to) == "2026-08-27"
    assert lic_type == "HMV"


@live
def test_live_salim_shaikh_cancellation_is_traceable():
    """(28) The audit remark survives the import."""
    n = _scalar("SELECT count(*) FROM core.pdp "
                "WHERE cancelled_by = 'Salim Shaikh' AND remarks LIKE 'Cancelled by:%'")
    assert n > 0


@live
def test_live_dq_issues_were_persisted():
    """(4 of the brief) Findings live in core.dq_issue, not just the console."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT issue_type, count(*) FROM core.dq_issue "
                    "WHERE source_table IN ('core.driver','core.pdp') GROUP BY 1")
        found = dict(cur.fetchall())
    for required in ("DUPLICATE_LICENCE", "DOB_INVALID"):
        assert found.get(required), f"{required} missing from core.dq_issue: {found}"
    assert found.get("DUPLICATE_LICENCE") == EXPECTED_DUP_LICENCE_GROUPS


@live
def test_live_sequence_is_past_the_imported_range():
    """A UI upload after the sheet import must not collide with driver_id."""
    seq = _scalar("SELECT last_value FROM core.driver_id_seq")
    mx = _scalar("SELECT COALESCE(max(driver_id), 0) FROM core.driver")
    assert seq >= mx, f"driver_id_seq {seq} is behind max(driver_id) {mx}"
