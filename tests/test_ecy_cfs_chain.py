"""ECY→CFS chain tests (Phase 7, F-Y1 lifecycle).

Router contract + chain shaping against a fake service (no DB), plus a lock test
that the rebuild SQL encodes every anomaly code the service documents.

The client-document realities asserted here:
  * the hero chain ONEU2122848 (ECY-out 01/07 10:00 -> CFS-in 14:00 -> CFS-out
    07/07 08:16) reports transit 4 h and a complete 3-leg chain;
  * COSU4663595 (duplicate CFS-In, two CFS-Outs) is FLAGGED, not silently
    absorbed — the audit's headline data-quality gap;
  * the terminal export gate-in leg is reported as absent-from-corpus rather
    than fabricated.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.routers import cfs_ecy as R
from services.cfs_ecy.chain_service import ANOMALY_LABELS, EcyCfsChainService

HERO = {
    "id": 1, "container_number": "ONEU2122848",
    "ecy_out_ts": "2026-07-01T10:00:00+05:30", "cfs_in_ts": "2026-07-01T14:00:00+05:30",
    "cfs_out_ts": "2026-07-07T08:16:00+05:30", "ecy_in_ts": None,
    "transit_hours": 4.0, "dwell_hours": 138.27, "cycle_hours": 142.27,
    "chain_status": "COMPLETE", "legs_present": 3, "event_count": 3,
    "has_anomaly": False, "anomaly_codes": [], "anomaly_detail": {},
}
ANOMALOUS = {
    "id": 2, "container_number": "COSU4663595",
    "ecy_out_ts": "2026-07-01T09:00:00+05:30", "cfs_in_ts": "2026-07-03T10:30:00+05:30",
    "cfs_out_ts": "2026-07-08T08:32:00+05:30", "ecy_in_ts": None,
    "transit_hours": 49.5, "dwell_hours": 118.03, "cycle_hours": 167.53,
    "chain_status": "COMPLETE", "legs_present": 3, "event_count": 5,
    "has_anomaly": True, "anomaly_codes": ["DUPLICATE_IN", "MULTI_OUT", "LONG_TRANSIT"],
    "anomaly_detail": {"cfs_in_count": 2, "cfs_out_count": 2},
}


class FakeChainRepo:
    def __init__(self):
        self.rows = {r["container_number"]: dict(r) for r in (HERO, ANOMALOUS)}
        self.rebuilt = 0

    async def rebuild(self):
        self.rebuilt += 1
        return {"chains": len(self.rows), "complete": 2, "anomalies": 1}

    async def list_chains(self, *, filters, limit, offset):
        rows = list(self.rows.values())
        if filters.get("container_number"):
            rows = [r for r in rows if r["container_number"] == filters["container_number"]]
        if filters.get("chain_status"):
            rows = [r for r in rows if r["chain_status"] == filters["chain_status"]]
        if filters.get("anomaly_only"):
            rows = [r for r in rows if r["has_anomaly"]]
        if filters.get("anomaly_code"):
            rows = [r for r in rows if filters["anomaly_code"] in r["anomaly_codes"]]
        return [dict(r) for r in rows[offset:offset + limit]]

    async def count_chains(self, *, filters):
        return len(await self.list_chains(filters=filters, limit=10_000, offset=0))

    async def get_chain(self, cn):
        r = self.rows.get(cn.strip().upper())
        return dict(r) if r else None

    async def stats(self):
        return {"chains": 2, "complete_chains": 2, "partial_chains": 0,
                "anomaly_chains": 1, "avg_transit_hours": 26.75,
                "avg_dwell_hours": 128.15, "avg_cycle_hours": 154.9,
                "median_cycle_hours": 154.9,
                "by_anomaly": [{"code": "DUPLICATE_IN", "chains": 1}],
                "last_rebuilt_at": None}


@pytest.fixture()
def svc():
    return EcyCfsChainService(repository=FakeChainRepo())


@pytest.mark.asyncio
async def test_hero_chain_has_named_legs_and_durations(svc):
    chain = await svc.get_chain("oneu2122848")
    assert chain["chain_status"] == "COMPLETE"
    assert chain["transit_hours"] == 4.0          # ECY-out 10:00 -> CFS-in 14:00
    legs = {l["leg"]: l for l in chain["legs"]}
    assert legs["ECY_GATE_OUT"]["present"] and legs["CFS_GATE_IN"]["present"]
    assert legs["CFS_GATE_OUT"]["present"]
    assert legs["ROAD_MOVEMENT"]["duration_hours"] == 4.0
    assert legs["CFS_DWELL"]["duration_hours"] == 138.27


@pytest.mark.asyncio
async def test_terminal_export_leg_is_reported_absent_not_fabricated(svc):
    chain = await svc.get_chain("ONEU2122848")
    export_leg = [l for l in chain["legs"] if l["leg"] == "TERMINAL_EXPORT_GATE_IN"][0]
    assert export_leg["present"] is False
    assert "not in corpus" in export_leg["note"]


@pytest.mark.asyncio
async def test_planted_anomaly_is_flagged_with_readable_labels(svc):
    chain = await svc.get_chain("COSU4663595")
    assert chain["has_anomaly"] is True
    assert set(chain["anomaly_codes"]) == {"DUPLICATE_IN", "MULTI_OUT", "LONG_TRANSIT"}
    assert any("more than one CFS gate-IN" in l for l in chain["anomaly_labels"])
    assert chain["anomaly_detail"]["cfs_out_count"] == 2


@pytest.mark.asyncio
async def test_list_filters_by_anomaly(svc):
    res = await svc.list_chains({"anomaly_only": True}, limit=50, offset=0)
    assert res["total"] == 1
    assert res["items"][0]["container_number"] == "COSU4663595"

    res = await svc.list_chains({"anomaly_code": "MULTI_OUT"}, limit=50, offset=0)
    assert res["total"] == 1

    res = await svc.list_chains({"anomaly_code": "OUT_BEFORE_IN"}, limit=50, offset=0)
    assert res["total"] == 0


@pytest.mark.asyncio
async def test_rebuild_is_callable_and_reports_counts(svc):
    res = await svc.rebuild()
    assert res["chains"] == 2 and res["anomalies"] == 1 and "ms" in res


def test_rebuild_sql_encodes_every_documented_anomaly_code():
    """The service's label table and the rebuild SQL must not drift apart."""
    from services.cfs_ecy.chain_repository import _REBUILD

    for code in ANOMALY_LABELS:
        assert f"'{code}'" in _REBUILD, code


def test_migration_defines_the_chain_table():
    sql = Path("infra/postgres/v3/0114_ecy_cfs_chain.sql").read_text()
    assert re.search(r"CREATE TABLE IF NOT EXISTS\s+core\.ecy_cfs_chain", sql, re.I)
    for col in ("ecy_out_ts", "cfs_in_ts", "cfs_out_ts", "transit_hours",
                "dwell_hours", "cycle_hours", "anomaly_codes", "chain_status"):
        assert col in sql, col


# ---------------------------------------------------------------------- router
@pytest.fixture()
def client(svc):
    app = FastAPI()
    app.include_router(R.router)
    app.dependency_overrides[R.get_chain_service] = lambda: svc
    return TestClient(app)


def test_router_chain_endpoints(client):
    r = client.get("/api/cfs-ecy/chains")
    assert r.status_code == 200 and r.json()["count"] == 2

    r = client.get("/api/cfs-ecy/chains", params={"anomaly_only": True})
    assert r.status_code == 200 and r.json()["items"][0]["container_number"] == "COSU4663595"

    r = client.get("/api/cfs-ecy/chains/stats")
    assert r.status_code == 200 and r.json()["anomaly_chains"] == 1
    assert "DUPLICATE_IN" in r.json()["anomaly_labels"]

    r = client.get("/api/cfs-ecy/chains/ONEU2122848")
    assert r.status_code == 200 and len(r.json()["legs"]) == 6

    r = client.get("/api/cfs-ecy/chains/ZZZU0000000")
    assert r.status_code == 404 and r.json()["detail"]["error"] == "chain_not_found"

    r = client.post("/api/cfs-ecy/chains/rebuild")
    assert r.status_code == 200 and r.json()["chains"] == 2
