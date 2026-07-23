"""Tests for Performance Data Upload Management (module 12 sub-module).

Three layers, mirroring tests/test_cfs_ecy.py:
  1. Pure parser/validator tests (no DB) — CSV parse, template check, bad
     date/number, unknown terminal, JN_PORT/TOTAL roll-ups, monthly + LDB.
  2. Router tests via Starlette TestClient (template download, type validation).
  3. Opt-in real-DB E2E — validate → import → verify perf_* rows → idempotent
     re-import → invalid rollback, auto-skipped when Postgres is unreachable.

All values used are REAL JNPA figures (from the 26-05-2026 Daily Status Report),
stamped to an unused date so the net-new insert path is exercised. No dummy data.
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.performance import upload_parsers as P  # noqa: E402

# Real JNPA daily-status values (26-05-2026 PDF) on an unused date.
_DAILY_ROWS = [
    "2026-06-05,NSFT,2,1775,1748,3523,5859,6729,5720,18308,23433,78.13,1153,1988,1588,1428,3016,1104,548,556",
    "2026-06-05,NSICT,4,2149,3997,6146,7486,2684,1753,11923,22638,52.67,1695,1667,636,1458,2094,772,354,418",
    "2026-06-05,GTI,3,9620,6203,15823,17244,8771,3140,29155,34354,84.87,3443,12737,2728,2328,5056,980,445,535",
]


def _daily_csv(rows=_DAILY_ROWS) -> bytes:
    return (",".join(P.DAILY_STATUS_COLS) + "\n" + "\n".join(rows)).encode()


def _parse(report_type: str, content: bytes) -> P.ParseResult:
    header, rows = P.read_rows(content, "f.csv")
    return P.parse(report_type, header, rows)


# --------------------------------------------------------------- parser units
def test_template_has_header():
    for rt in ("daily_status", "monthly_teu", "ldb_report"):
        csv = P.template_csv(rt)
        assert csv.splitlines()[0]  # header present
        assert "," in csv.splitlines()[0]


def test_daily_parse_records_and_rollups():
    res = _parse("daily_status", _daily_csv())
    assert res.errors == []
    assert not res.rejected
    # traffic: 3 terminals + JN_PORT rollup; status: 3 + TOTAL; snapshot: 1
    traffic = {r["terminal_code"]: r for r in res.records["traffic"]}
    status = {r["terminal_code"]: r for r in res.records["status"]}
    assert set(traffic) == {"NSFT", "NSICT", "APMT", "JN_PORT"}   # GTI -> APMT
    assert traffic["JN_PORT"]["total_teus"] == 3523 + 6146 + 15823 == 25492
    assert traffic["APMT"]["imp_teus"] == 9620       # GTI normalized to APMT
    assert "TOTAL" in status
    assert len(res.records["snapshot"]) == 1
    assert res.records["snapshot"][0]["report_date"].isoformat() == "2026-06-05"


def test_daily_bad_values_flagged():
    bad = _daily_csv(["2026-13-40,ZZZ,notanum,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"])
    res = _parse("daily_status", bad)
    codes = {e["error_code"] for e in res.errors}
    assert "invalid_date" in codes
    assert "invalid_number" in codes
    assert "unknown_terminal" in codes


def test_wrong_template_rejected():
    res = _parse("daily_status", b"foo,bar\n1,2")
    assert res.rejected
    assert res.errors[0]["error_code"] == "missing_columns"


def test_monthly_parse_rollup_and_fiscal_year():
    csv = (",".join(P.MONTHLY_TEU_COLS) + "\n"
           "2026-01-01,NSFT,7,2148,2132,4280\n"
           "2026-01-01,NSICT,57,56437,61531,117968").encode()
    res = _parse("monthly_teu", csv)
    assert res.errors == []
    by = {r["terminal_code"]: r for r in res.records["monthly"]}
    assert by["NSFT"]["total_teus"] == 4280
    assert by["JN_PORT"]["total_teus"] == 4280 + 117968      # rollup
    assert by["NSFT"]["fiscal_year"] == "FY-2025-26"         # Jan -> FY starting prev Apr


def test_ldb_parse():
    csv = (",".join(P.LDB_REPORT_COLS) + "\n"
           "2026-03-01,NSFT,IMPORT,OVERALL,22.8,29.3\n"
           "2026-03-01,BMCT,EXPORT,OVERALL,78.6,74.5").encode()
    res = _parse("ldb_report", csv)
    assert res.errors == []
    rows = {(r["terminal_code"], r["cycle"]): r for r in res.records["ldb_port_dwell"]}
    assert float(rows[("NSFT", "IMPORT")]["dwell_hours"]) == 22.8
    assert float(rows[("BMCT", "EXPORT")]["dwell_hours_prev"]) == 74.5


def test_ldb_invalid_enum_flagged():
    csv = (",".join(P.LDB_REPORT_COLS) + "\n2026-03-01,NSFT,SIDEWAYS,OVERALL,1,2").encode()
    res = _parse("ldb_report", csv)
    assert any(e["error_code"] == "invalid_cycle" for e in res.errors)


# --------------------------------------------------------------- router (light)
def _client():
    import types
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from gateway.routers import performance_upload as pu

    app = FastAPI()
    app.state.gw = types.SimpleNamespace(cfg=types.SimpleNamespace(postgres_dsn=os.environ["POSTGRES_DSN"]))
    app.include_router(pu.router)
    return TestClient(app)


def test_template_endpoint_and_type_validation():
    with _client() as c:
        r = c.get("/api/performance/templates/daily_status")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        assert r.text.splitlines()[0].startswith("report_date")
        assert c.get("/api/performance/templates/bogus").status_code == 400


# --------------------------------------------------------------- opt-in real DB
def _pg_reachable(host="127.0.0.1", port=5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


_REAL_DSN = "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres"


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_validate_import_idempotent_and_rollback():
    import asyncio

    from sqlalchemy import text

    from gateway.performance_ext import ensure_performance_schema
    from gateway.performance_upload_ext import ensure_performance_upload_schema
    from jnpa_shared.db import get_engine, dispose_all
    from services.performance import UploadService

    async def run():
        await ensure_performance_schema(_REAL_DSN)
        await ensure_performance_upload_schema(_REAL_DSN)
        eng = get_engine(_REAL_DSN)

        async def clean():
            async with eng.begin() as c:
                for t in ("perf_daily_traffic", "perf_daily_terminal_status", "perf_daily_snapshot"):
                    await c.execute(text(f"delete from jnpa.{t} where report_date='2026-06-05'"))

        try:
            await clean()
            svc = UploadService(dsn=_REAL_DSN)
            csv = _daily_csv()

            # validate — clean
            v = await svc.validate("daily_status", csv, "e2e.csv", "pytest")
            assert v["valid"] is True and v["summary"]["errors"] == 0

            # import — net new
            im = await svc.import_file("daily_status", csv, "e2e.csv", "pytest")
            assert im["status"] == "IMPORTED"
            assert im["inserted"] == 9 and im["skipped"] == 0

            async with eng.connect() as c:
                jp = (await c.execute(text(
                    "select total_teus from jnpa.perf_daily_traffic "
                    "where report_date='2026-06-05' and terminal_code='JN_PORT' and period='DAY'"))).scalar()
                assert int(jp) == 25492                       # roll-up landed in the dashboard table

            # re-import of the SAME file — no duplicate rows, all refreshed in place
            im2 = await svc.import_file("daily_status", csv, "e2e.csv", "pytest")
            assert im2["inserted"] == 0 and im2["updated"] == 9

            # re-import of a CORRECTED report must actually correct the stored figures
            # (the previous ON CONFLICT DO NOTHING silently discarded the correction).
            corrected = csv.replace(b"5859,6729", b"6000,6729")
            assert corrected != csv
            im3 = await svc.import_file("daily_status", corrected, "e2e-rev2.csv", "pytest")
            assert im3["status"] == "IMPORTED" and im3["updated"] >= 1
            async with eng.connect() as c:
                yi, src = (await c.execute(text(
                    "select yard_import_teus, source_file from jnpa.perf_daily_terminal_status "
                    "where report_date='2026-06-05' and terminal_code='NSFT'"))).first()
                assert int(yi) == 6000                        # corrected value won
                assert src == "e2e-rev2.csv"                  # provenance points at the new file

            # invalid file — rejected, NOTHING written (rollback/refuse)
            bad = _daily_csv(["2026-13-40,ZZZ,x,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17"])
            ib = await svc.import_file("daily_status", bad, "bad.csv", "pytest")
            assert ib["status"] == "REJECTED" and ib["inserted"] == 0

            # history recorded the attempts
            hist = await svc.list_uploads({}, limit=10, offset=0)
            assert hist["total"] >= 3
        finally:
            await clean()
            await dispose_all()

    asyncio.run(run())
