"""Tests for the Performance & Daily Reports surface (/api/performance) — module 12.

Three layers, mirroring test_cfs_ecy / test_driver_master:

* Pure data-layer checks on the PDF importer's parsing helpers (number / percent /
  terminal-alias / date normalisation) — no DB, no PDF.
* Full router reads via Starlette's TestClient with the DB repository swapped for an
  in-memory fake through ``app.dependency_overrides`` — so router logic (envelopes,
  400/404, filters) is exercised deterministically with no Postgres.
* A final REAL-DB integration test (schema ensure + KPI read) that is skipped
  automatically when Postgres is unreachable (compose publishes it on host 5433).
"""
from __future__ import annotations

import datetime as dt
import os
import socket
import sys
from pathlib import Path
from typing import Any, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Unroutable DSN so any accidental real-DB path fails fast (the fake bypasses it).
os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from scripts.import_performance_reports import (  # noqa: E402
    as_int, daily_date_from_name, ldb_month_from_name, norm_terminal, num, pct,
)
from services.performance import PerformanceService  # noqa: E402


# =====================================================================
# 1. Pure parser unit tests (no DB, no PDF)
# =====================================================================
def test_num_handles_commas_dashes_newlines():
    assert num("18,992.00") == 18992.0
    assert num("1,377,355") == 1377355.0
    assert num("17,228,831.8\n0") == 17228831.80   # tonnage YEAR cell wraps a digit
    assert num("-") is None and num("") is None and num(None) is None


def test_pct_strips_percent():
    assert pct("84.87%") == 84.87
    assert pct("7.63%") == 7.63
    assert pct("-") is None


def test_as_int_rounds():
    assert as_int("25") == 25
    assert as_int("3.0") == 3
    assert as_int(None) is None


def test_norm_terminal_reconciles_aliases():
    # GTI ≡ APMT, BMCTPL ≡ BMCT are the critical cross-report reconciliations.
    assert norm_terminal("GTI") == "APMT"
    assert norm_terminal("APMT") == "APMT"
    assert norm_terminal("BMCTPL") == "BMCT"
    assert norm_terminal("NSFT") == "NSFT"
    assert norm_terminal("JN Port") == "JN_PORT"
    # full name with code in parentheses (weather / performance-index tables)
    assert norm_terminal("Gateway Terminals India (GTI)") == "APMT"
    assert norm_terminal("Nhava Sheva Freeport Terminal (NSFT)") == "NSFT"
    assert norm_terminal("") is None and norm_terminal(None) is None


def test_daily_date_from_name_both_separators():
    assert daily_date_from_name("Daily Status Report 03.02.2026.pdf") == dt.date(2026, 2, 3)
    assert daily_date_from_name("Daily_Status_Report_26-05-2026.pdf") == dt.date(2026, 5, 26)
    assert daily_date_from_name("nope.pdf") is None


def test_ldb_month_from_name():
    assert ldb_month_from_name("NLDS_LDB_Full Analysis_March_2026.pdf") == dt.date(2026, 3, 1)


# =====================================================================
# 2. Router tests with an in-memory fake repository
# =====================================================================
_KPI = {
    "report_date": "2026-05-26", "prev_report_date": "2026-05-25",
    "metrics": {"total_teus": 33603.0, "total_tonnes": 408104.38, "vessel_calls": 25,
                "yard_occupancy_pct": 61.06, "gate_total_teus": 18970.0,
                "total_pendency_teus": 50851.0, "reefer_available_slots": 3336},
    "deltas": {"total_teus": 19454.0},
}
_TRAFFIC = [{"report_date": "2026-05-26", "terminal_code": "NSFT", "period": "DAY",
             "vessels": 2, "imp_teus": 1775, "exp_teus": 1748, "total_teus": 3523,
             "rakes": None, "rail_dis_teus": None, "rail_ldg_teus": None, "rail_total_teus": None}]
_DWELL = [{"report_month": "2026-03-01", "terminal_code": "APMT", "cycle": "IMPORT",
           "segment": "OVERALL", "dwell_hours": 18.8, "dwell_hours_prev": 26.4}]


class FakePerformanceRepo:
    """In-memory stand-in for PerformanceRepository with identical method contracts."""

    async def terminals(self):
        return [{"code": "NSFT", "full_name": "Nhava Sheva Freeport Terminal",
                 "operator": "NSFT", "terminal_type": "CONTAINER", "is_container": True,
                 "aliases": ["NSFT"], "sort_order": 10},
                {"code": "APMT", "full_name": "APM Terminals", "operator": "APM",
                 "terminal_type": "CONTAINER", "is_container": True,
                 "aliases": ["GTI", "APM"], "sort_order": 40}]

    async def report_dates(self, limit: int = 60):
        return ["2026-05-26", "2026-05-25"]

    async def latest_report_date(self):
        return dt.date(2026, 5, 26)

    async def prev_report_date(self, d):
        return dt.date(2026, 5, 25)

    async def ldb_months(self):
        return ["2026-03-01"]

    async def kpi(self, report_date):
        return None if report_date == dt.date(1900, 1, 1) else _KPI

    async def daily_bundle(self, d):
        if d == dt.date(2026, 5, 26):
            return {"snapshot": {"report_date": "2026-05-26", "as_of_ts": None,
                                 "source_file": "x.pdf"},
                    "traffic": _TRAFFIC, "tonnage": [], "status": [], "vessels": []}
        return None

    async def list_traffic(self, filters, *, sort, direction, limit, offset):
        return _TRAFFIC, len(_TRAFFIC)

    async def list_status(self, filters, *, limit, offset):
        return [], 0

    async def list_vessels(self, filters, *, limit, offset):
        return [], 0

    async def list_monthly(self, filters, *, sort, direction, limit, offset):
        return [{"month_date": "2025-04-01", "terminal_code": "JN_PORT",
                 "total_teus": 667922}], 1

    async def trends(self, metric, *, grain, terminal, date_from, date_to):
        return [{"t": "2026-05-26", "terminal_code": "JN_PORT", "value": 33603.0}]

    async def daily_series(self, date_from, date_to):
        return [{"day": "2026-05-26", "total_teus": 33603.0, "gate_in_teus": 9089.0,
                 "gate_out_teus": 9881.0, "yard_occupancy_pct": 61.06}]

    async def ldb_port_dwell(self, filters):
        return list(_DWELL)

    async def ldb_facility_dwell(self, filters, *, limit, offset):
        return [{"facility_type": "CFS", "facility_name": "CWC Polaris logistics park",
                 "dwell_hours": 99.6, "dwell_hours_prev": 89.4}], 1

    async def ldb_congestion(self, filters):
        return [{"cycle": "IMPORT", "cluster_no": 1, "cluster_name": "JNPA Area",
                 "cfs_count": 1, "pct_containers": 7.63, "congestion_level": "HIGH"}]

    async def ldb_routes(self, filters):
        return [{"cycle": "EXPORT", "transport_mode": "TRAIN", "route_name": "Vadodra Route",
                 "pct_share": 35.0}]

    async def ldb_weather(self, filters):
        return [{"terminal_code": "NSFT", "cycle": "IMPORT", "weather": "NORMAL",
                 "dwell_hours": 22.8}]


@pytest.fixture()
def client():
    from gateway.main import app
    from gateway.routers import performance as prouter
    fake = PerformanceService(repository=FakePerformanceRepo())
    app.dependency_overrides[prouter.get_service] = lambda: fake
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(prouter.get_service, None)


def test_terminals_envelope(client):
    r = client.get("/api/performance/terminals")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert {t["code"] for t in body["items"]} == {"NSFT", "APMT"}


def test_kpi_returns_metrics_and_deltas(client):
    r = client.get("/api/performance/kpi")
    assert r.status_code == 200
    body = r.json()
    assert body["report_date"] == "2026-05-26"
    assert body["metrics"]["total_teus"] == 33603.0
    assert body["deltas"]["total_teus"] == 19454.0


def test_daily_bundle_ok_and_404(client):
    ok = client.get("/api/performance/daily?date=2026-05-26")
    assert ok.status_code == 200
    assert ok.json()["traffic"][0]["terminal_code"] == "NSFT"
    missing = client.get("/api/performance/daily?date=2020-01-01")
    assert missing.status_code == 404
    assert missing.json()["detail"]["error"] == "report_not_found"


def test_traffic_list_envelope(client):
    r = client.get("/api/performance/daily/traffic?period=DAY")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"items", "total", "limit", "offset", "count"}
    assert body["items"][0]["total_teus"] == 3523


def test_traffic_invalid_period_400(client):
    r = client.get("/api/performance/daily/traffic?period=WEEK")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_period"


def test_monthly_teu(client):
    r = client.get("/api/performance/monthly-teu?terminal=JN_PORT")
    assert r.status_code == 200
    assert r.json()["items"][0]["total_teus"] == 667922


def test_trends(client):
    r = client.get("/api/performance/trends?metric=total_teus&grain=daily")
    assert r.status_code == 200
    assert r.json()["series"][0]["value"] == 33603.0


def test_trends_invalid_grain_400(client):
    r = client.get("/api/performance/trends?grain=weekly")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_grain"


def test_ldb_dwell_and_alias(client):
    r = client.get("/api/performance/dwell?segment=OVERALL&cycle=IMPORT")
    assert r.status_code == 200
    items = r.json()["items"]
    assert items[0]["terminal_code"] == "APMT" and items[0]["dwell_hours"] == 18.8


def test_ldb_dwell_invalid_cycle_400(client):
    r = client.get("/api/performance/dwell?cycle=SIDEWAYS")
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "invalid_cycle"


def test_cfs_icd_and_congestion(client):
    fac = client.get("/api/performance/cfs-icd?facility_type=CFS")
    assert fac.status_code == 200 and fac.json()["items"][0]["facility_type"] == "CFS"
    cong = client.get("/api/performance/congestion?cycle=IMPORT")
    assert cong.status_code == 200 and cong.json()["items"][0]["congestion_level"] == "HIGH"


def test_stats_overview(client):
    r = client.get("/api/performance/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 1 and body["daily"][0]["total_teus"] == 33603.0


# =====================================================================
# 3. Opt-in real-DB integration test
# =====================================================================
def _pg_reachable(host="127.0.0.1", port=5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_schema_and_kpi():
    import asyncio
    from gateway.performance_ext import ensure_performance_schema

    dsn = "postgresql+asyncpg://postgres:TempPass123!@localhost:5433/postgres"

    async def go():
        await ensure_performance_schema(dsn)          # idempotent
        svc = PerformanceService(dsn=dsn)
        terms = await svc.terminals()
        assert terms["count"] >= 6                     # seeded dimension
        kpi = await svc.kpi(None)                       # latest day (data imported)
        # KPI is None only on an empty DB; when the importer has run it has metrics.
        if kpi is not None:
            assert "total_teus" in kpi["metrics"]

    asyncio.run(go())
