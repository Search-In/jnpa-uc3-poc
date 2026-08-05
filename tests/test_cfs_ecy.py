"""Tests for the CFS-ECY CODECO surface (/api/cfs-ecy) — UC-III module 13.

Three layers, mirroring test_cargo / test_driver_master:

* Pure data-layer checks on the ingestion cleaner (ISO-6346 validation, mode
  normalization, timestamp parsing, in-file duplicate detection) — no DB.
* Full router reads via Starlette's TestClient with the DB repository swapped for
  an in-memory fake through ``app.dependency_overrides`` — so router logic
  (list/filter/stats/timeline, 400/404) is exercised deterministically with no
  Postgres.
* A final REAL-DB integration test (schema ensure + idempotent ingest + dwell)
  that is skipped automatically when Postgres is unreachable (compose publishes it
  on host 5433).
"""
from __future__ import annotations

import os
import socket
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
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

from jnpa_shared.iso6346 import is_valid_container_no, with_check_digit  # noqa: E402
from services.cfs_ecy import CfsEcyService  # noqa: E402

# Deterministic valid ISO-6346 numbers for the fixtures.
CN_CFS_A = with_check_digit("AAAU100000")
CN_CFS_B = with_check_digit("BBBU200000")
CN_ECY_OUT = with_check_digit("CCCU300000")
CN_ECY_IN = with_check_digit("DDDU400000")
assert all(is_valid_container_no(c) for c in (CN_CFS_A, CN_CFS_B, CN_ECY_OUT, CN_ECY_IN))

IST = timezone(timedelta(hours=5, minutes=30))
_T0 = datetime(2026, 7, 1, 10, 0, tzinfo=IST)


def _ev(facility, cn, mode, ts, iso_valid=True, _id=0):
    return {"id": _id, "facility_type": facility, "container_number": cn,
            "iso_valid": iso_valid, "event_ts": ts, "mode": mode,
            "source": "CODECO", "source_file": f"{facility}-CODECO.xlsx",
            "created_at": _T0}


# Fixture dataset: 2 full CFS cycles (dwell 24h & 48h), 1 ECY OUT, 1 ECY IN.
_ROWS = [
    _ev("CFS", CN_CFS_A, "IN", _T0, _id=1),
    _ev("CFS", CN_CFS_A, "OUT", _T0 + timedelta(hours=24), _id=2),
    _ev("CFS", CN_CFS_B, "IN", _T0 + timedelta(days=1), _id=3),
    _ev("CFS", CN_CFS_B, "OUT", _T0 + timedelta(days=1, hours=48), _id=4),
    _ev("ECY", CN_ECY_OUT, "OUT", _T0 + timedelta(days=2), _id=5),
    _ev("ECY", CN_ECY_IN, "IN", _T0 + timedelta(days=2, hours=1), _id=6),
]


class FakeCfsEcyRepo:
    """In-memory stand-in for CfsEcyRepository with identical method contracts.
    Dwell mirrors the SQL view: CFS-only, first IN → last OUT."""

    def __init__(self, rows=None, cargo=None) -> None:
        self._rows = [dict(r) for r in (rows if rows is not None else _ROWS)]
        self._cargo = dict(cargo or {})  # container_number -> cargo row

    def _match(self, r: Mapping[str, Any], f: Mapping[str, Any]) -> bool:
        if f.get("facility_type") and r["facility_type"] != f["facility_type"]:
            return False
        if f.get("mode") and r["mode"] != f["mode"]:
            return False
        if f.get("container") and f["container"].upper() not in r["container_number"].upper():
            return False
        if f.get("ts_from") and r["event_ts"] < f["ts_from"]:
            return False
        if f.get("ts_to") and r["event_ts"] > f["ts_to"]:
            return False
        return True

    def _filtered(self, f):
        return [r for r in self._rows if self._match(r, f)]

    async def list_movements(self, filters, *, sort, direction, limit, offset):
        rows = self._filtered(filters)
        rev = str(direction).lower() != "asc"
        rows.sort(key=lambda r: (r.get(sort if sort in r else "event_ts"), r["id"]), reverse=rev)
        return [dict(r) for r in rows[offset:offset + limit]]

    async def count(self, filters):
        return len(self._filtered(filters))

    def _dwell_rows(self, f):
        by = defaultdict(list)
        for r in self._filtered(f):
            by[(r["container_number"], r["facility_type"])].append(r)
        out = []
        for (cn, fac), evs in by.items():
            ins = [e["event_ts"] for e in evs if e["mode"] == "IN"]
            outs = [e["event_ts"] for e in evs if e["mode"] == "OUT"]
            dwell = None
            if fac == "CFS" and ins and outs and max(outs) >= min(ins):
                dwell = round((max(outs) - min(ins)).total_seconds() / 3600.0, 2)
            out.append({"container_number": cn, "facility_type": fac,
                        "first_in_ts": min(ins) if ins else None,
                        "last_out_ts": max(outs) if outs else None,
                        "in_events": len(ins), "out_events": len(outs),
                        "dwell_hours": dwell})
        return out

    async def stats(self, filters):
        rows = self._filtered(filters)
        by = defaultdict(lambda: [0, 0])
        for r in rows:
            by[(r["container_number"], r["facility_type"])][0 if r["mode"] == "IN" else 1] += 1
        active = sum(1 for v in by.values() if v[0] > v[1])
        return {
            "total_in": sum(1 for r in rows if r["mode"] == "IN"),
            "total_out": sum(1 for r in rows if r["mode"] == "OUT"),
            "container_count": len({r["container_number"] for r in rows}),
            "total_events": len(rows),
            "iso_invalid": sum(1 for r in rows if not r["iso_valid"]),
            "active_containers": active,
        }

    async def dwell_summary(self, filters):
        if filters.get("facility_type") and filters["facility_type"] != "CFS":
            return {"average_dwell_hours": None, "median_dwell_hours": None, "dwell_count": 0}
        vals = [d["dwell_hours"] for d in self._dwell_rows({"facility_type": "CFS"})
                if d["dwell_hours"] is not None]
        if not vals:
            return {"average_dwell_hours": None, "median_dwell_hours": None, "dwell_count": 0}
        return {"average_dwell_hours": round(statistics.mean(vals), 2),
                "median_dwell_hours": round(statistics.median(vals), 2),
                "dwell_count": len(vals)}

    async def daily_throughput(self, filters):
        by = defaultdict(lambda: [0, 0])
        for r in self._filtered(filters):
            day = str(r["event_ts"].astimezone(IST).date())
            by[day][0 if r["mode"] == "IN" else 1] += 1
        return [{"day": d, "in_count": c[0], "out_count": c[1]} for d, c in sorted(by.items())]

    async def container_events(self, cn):
        rows = [dict(r) for r in self._rows if r["container_number"] == cn]
        rows.sort(key=lambda r: (r["event_ts"], r["id"]))
        return rows

    async def container_dwell(self, cn):
        return [d for d in self._dwell_rows({}) if d["container_number"] == cn]

    async def cargo_lifecycle(self, cn):
        return self._cargo.get(cn)

    async def dwell_report(self, filters, *, limit, offset):
        rows = [d for d in self._dwell_rows({"facility_type": "CFS"})
                if d["dwell_hours"] is not None]
        rows.sort(key=lambda d: d["dwell_hours"], reverse=True)
        return rows[offset:offset + limit], len(rows)


@pytest.fixture()
def client():
    """App-bound TestClient with the CFS-ECY service backed by a fake repo."""
    from gateway.main import app
    from gateway.routers import cfs_ecy as cfs_ecy_router

    fake = CfsEcyService(repository=FakeCfsEcyRepo())
    app.dependency_overrides[cfs_ecy_router.get_service] = lambda: fake
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(cfs_ecy_router.get_service, None)


def _client_with(cargo=None):
    from gateway.main import app
    from gateway.routers import cfs_ecy as cfs_ecy_router
    fake = CfsEcyService(repository=FakeCfsEcyRepo(cargo=cargo))
    app.dependency_overrides[cfs_ecy_router.get_service] = lambda: fake
    return app, cfs_ecy_router, TestClient(app)


# --------------------------------------------------------------- pure data layer
def test_iso6346_validation_of_fixtures():
    assert is_valid_container_no(CN_CFS_A)
    assert not is_valid_container_no("MAEU6123450")  # right shape, wrong check digit


def test_ingestion_cleaner_normalizes_and_validates():
    from scripts.import_cfs_ecy_codeco import clean_row, norm_mode, parse_ts

    assert norm_mode("In") == "IN" and norm_mode("out") == "OUT" and norm_mode("x") is None
    ts = parse_ts("01/07/2026 14:00")
    assert ts is not None and ts.year == 2026 and ts.month == 7 and ts.day == 1 and ts.hour == 14
    assert ts.utcoffset() == timedelta(hours=5, minutes=30)  # IST
    rec, reason = clean_row({"Container Number": CN_CFS_A, "Timestamp": "01/07/2026 14:00",
                             "Mode": "In"}, "CFS", "CFS-CODECO.xlsx")
    assert reason is None and rec["mode"] == "IN" and rec["facility_type"] == "CFS"
    assert rec["iso_valid"] is True
    bad, why = clean_row({"Container Number": None, "Timestamp": "01/07/2026 14:00",
                          "Mode": "In"}, "CFS", "f")
    assert bad is None and why == "missing_container"


@pytest.mark.skipif(not (Path("/Users/pandurangdhage/Downloads/Digital Twin/Data/13-CFS-ECY")).exists(),
                    reason="JNPA CFS-ECY data folder not present")
def test_ingestion_report_matches_source_data():
    """Data-quality assertions against the REAL JNPA files (no DB)."""
    from scripts.import_cfs_ecy_codeco import DEFAULT_DATA_DIR, build_report

    rep = build_report(DEFAULT_DATA_DIR, None)
    # 968 CFS + 961 ECY = 1929 raw; the 1 exact-dup CFS row is dropped -> 1928.
    assert len(rep["valid"]) == 1928
    assert len(rep["invalid"]) == 0
    assert rep["iso_invalid"] == 0            # 100% ISO-6346 valid
    dupes = sum(f["in_file_duplicates"] for f in rep["per_file"].values())
    assert dupes == 1                         # the single COSU… duplicate
    assert rep["per_file"]["CFS"]["raw_rows"] == 968
    assert rep["per_file"]["ECY"]["raw_rows"] == 961


# ------------------------------------------------------------------ list + filter
def test_list_all(client):
    r = client.get("/api/cfs-ecy/movements")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 6 and body["count"] == 6

def test_filter_by_facility(client):
    assert client.get("/api/cfs-ecy/movements", params={"facility": "CFS"}).json()["total"] == 4
    assert client.get("/api/cfs-ecy/movements", params={"facility": "ECY"}).json()["total"] == 2

def test_filter_by_mode(client):
    assert client.get("/api/cfs-ecy/movements", params={"mode": "IN"}).json()["total"] == 3
    assert client.get("/api/cfs-ecy/movements", params={"mode": "OUT"}).json()["total"] == 3

def test_filter_by_container(client):
    r = client.get("/api/cfs-ecy/movements", params={"container": CN_CFS_A})
    assert r.json()["total"] == 2
    assert all(i["container_number"] == CN_CFS_A for i in r.json()["items"])

def test_invalid_facility_400(client):
    assert client.get("/api/cfs-ecy/movements", params={"facility": "ICD"}).status_code == 400

def test_invalid_mode_400(client):
    assert client.get("/api/cfs-ecy/movements", params={"mode": "SIDEWAYS"}).status_code == 400


# --------------------------------------------------------------------- stats
def test_stats(client):
    s = client.get("/api/cfs-ecy/stats").json()
    assert s["total_in"] == 3 and s["total_out"] == 3 and s["total_events"] == 6
    assert s["container_count"] == 4
    assert s["active_containers"] == 1          # only the ECY IN-only container
    assert s["average_dwell_hours"] == 36.0     # mean(24, 48)
    assert s["median_dwell_hours"] == 36.0
    assert s["dwell_count"] == 2
    assert isinstance(s["daily_throughput"], list) and s["daily_throughput"]

def test_stats_ecy_has_no_dwell(client):
    s = client.get("/api/cfs-ecy/stats", params={"facility": "ECY"}).json()
    assert s["average_dwell_hours"] is None and s["dwell_count"] == 0


# --------------------------------------------------------------------- dwell
def test_dwell_report(client):
    d = client.get("/api/cfs-ecy/dwell").json()
    assert d["total"] == 2
    # longest dwell first
    assert d["items"][0]["dwell_hours"] == 48.0
    assert d["items"][1]["dwell_hours"] == 24.0
    assert all(i["facility_type"] == "CFS" for i in d["items"])


# --------------------------------------------------------------- container timeline
def test_container_timeline(client):
    r = client.get(f"/api/cfs-ecy/containers/{CN_CFS_A}")
    assert r.status_code == 200
    body = r.json()
    assert body["container_number"] == CN_CFS_A
    assert len(body["events"]) == 2
    assert body["dwell_hours"] == 24.0
    assert body["in_lifecycle"] is False and body["cargo"] is None

def test_container_timeline_with_cargo_lifecycle():
    app, router, c = _client_with(cargo={CN_CFS_A: {"container_number": CN_CFS_A,
                                                    "lifecycle_status": "RELEASED",
                                                    "customs_status": "CLEARED"}})
    try:
        with c:
            body = c.get(f"/api/cfs-ecy/containers/{CN_CFS_A}").json()
            assert body["in_lifecycle"] is True
            assert body["cargo"]["lifecycle_status"] == "RELEASED"
    finally:
        app.dependency_overrides.pop(router.get_service, None)

def test_container_timeline_404(client):
    assert client.get("/api/cfs-ecy/containers/ZZZU0000000").status_code == 404


# --------------------------------------------------- real-DB integration (opt-in)
def _pg_reachable(host: str = "127.0.0.1", port: int = 5433) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="Postgres not reachable on 5433")
def test_real_db_ingest_and_dwell():
    """Ensure schema, idempotently insert 2 CFS + 1 ECY events, verify counts +
    CFS dwell via the REAL repository/view. Re-running is a no-op (ON CONFLICT)."""
    import asyncio

    from gateway.cfs_ecy_ext import ensure_cfs_ecy_schema
    from jnpa_shared.db import get_engine
    from sqlalchemy import text

    dsn = os.environ.get(
        "CFS_ECY_TEST_DSN",
        os.environ.get("POSTGRES_DSN") if "127.0.0.1:1" not in os.environ.get("POSTGRES_DSN", "")
        else None) or "postgresql+asyncpg://postgres:TempPass123!@127.0.0.1:5433/postgres"
    cn = with_check_digit("TESU900001")
    t_in = datetime(2026, 7, 5, 8, 0, tzinfo=IST)
    t_out = t_in + timedelta(hours=30)
    rows = [
        {"facility_type": "CFS", "container_number": cn, "iso_valid": True,
         "event_ts": t_in, "mode": "IN", "source": "TEST", "source_file": "t"},
        {"facility_type": "CFS", "container_number": cn, "iso_valid": True,
         "event_ts": t_out, "mode": "OUT", "source": "TEST", "source_file": "t"},
        {"facility_type": "ECY", "container_number": cn, "iso_valid": True,
         "event_ts": t_in, "mode": "OUT", "source": "TEST", "source_file": "t"},
    ]
    ins = ("INSERT INTO core.cfs_ecy_movement "
           "(facility_type, container_number, iso_valid, event_ts, mode, source, source_file) "
           "VALUES (:facility_type,:container_number,:iso_valid,:event_ts,:mode,:source,:source_file) "
           "ON CONFLICT ON CONSTRAINT uq_cfs_ecy_movement DO NOTHING")

    async def run():
        get_engine.cache_clear()
        await ensure_cfs_ecy_schema(dsn)
        eng = get_engine(dsn)
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM core.cfs_ecy_movement WHERE container_number=:cn"),
                               {"cn": cn})
        # First insert: 3 new rows. Second insert: 0 (idempotent).
        n1 = 0
        async with eng.begin() as conn:
            for r in rows:
                n1 += (await conn.execute(text(ins), r)).rowcount or 0
        n2 = 0
        async with eng.begin() as conn:
            for r in rows:
                n2 += (await conn.execute(text(ins), r)).rowcount or 0
        async with eng.connect() as conn:
            cnt = (await conn.execute(text("SELECT count(*) n FROM core.cfs_ecy_movement "
                                           "WHERE container_number=:cn"), {"cn": cn})).scalar()
            dwell = (await conn.execute(text("SELECT dwell_hours FROM mart.v_cfs_ecy_dwell "
                                             "WHERE container_number=:cn AND facility_type='CFS'"),
                                        {"cn": cn})).scalar()
            ecy_dwell = (await conn.execute(text("SELECT dwell_hours FROM mart.v_cfs_ecy_dwell "
                                                 "WHERE container_number=:cn AND facility_type='ECY'"),
                                            {"cn": cn})).scalar()
        async with eng.begin() as conn:  # cleanup
            await conn.execute(text("DELETE FROM core.cfs_ecy_movement WHERE container_number=:cn"),
                               {"cn": cn})
        return n1, n2, cnt, dwell, ecy_dwell

    n1, n2, cnt, dwell, ecy_dwell = asyncio.run(run())
    assert n1 == 3          # 3 new rows inserted
    assert n2 == 0          # idempotent re-run inserts nothing
    assert cnt == 3
    assert float(dwell) == 30.0     # CFS dwell = OUT - IN = 30h
    assert ecy_dwell is None        # ECY dwell is NOT fabricated
