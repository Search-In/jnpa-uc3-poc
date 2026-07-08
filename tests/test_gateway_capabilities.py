"""Tests for the five Appendix-C capability routes on the gateway.

These boot the gateway in-process (Starlette TestClient) with a FakeHttp that
stubs NO upstreams, so every route exercises its in-process SYNTHETIC fallback —
proving the dashboard renders Empty-Container, Carbon, Auto-LEO/Customs,
Identity, and Parking even when the backing services are down. No docker stack.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (
    str(REPO_ROOT / "shared"),
    str(REPO_ROOT / "empty-container"),
    str(REPO_ROOT / "carbon"),
    str(REPO_ROOT / "gate-data"),
    str(REPO_ROOT / "identity"),
    str(REPO_ROOT / "parking"),
    str(REPO_ROOT),
):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402


class FakeHttp:
    """Upstream client that always fails -> routers take the SYNTHETIC path."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get(self, url: str, **kw):
        self.calls.append(url)
        import httpx
        raise httpx.ConnectError(f"down: {url}")

    async def post(self, url: str, **kw):
        self.calls.append(url)
        import httpx
        raise httpx.ConnectError(f"down: {url}")

    async def aclose(self):
        pass


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("KAFKA_BROKERS", "127.0.0.1:1")
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()
    import gateway.config as cfgmod
    importlib.reload(cfgmod)
    import gateway.main as mainmod
    importlib.reload(mainmod)
    c = TestClient(mainmod.app)
    c.__enter__()
    mainmod.app.state.gw.http = FakeHttp()
    yield c
    c.__exit__(None, None, None)


# --- Empty-container (C3) ---------------------------------------------------
def test_empty_allocations_synthetic(client):
    r = client.get("/api/empty/allocations")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] == "SYNTHETIC"
    assert body["count"] >= 1
    a = body["allocations"][0]
    assert {"demand_id", "supply_depot", "cargo_type", "est_trt_min"} <= set(a)


def test_empty_kpi_synthetic_returns_trt_kpi(client):
    r = client.get("/api/empty/kpi")
    assert r.status_code == 200
    kpi = r.json()["kpi"]
    assert kpi["key"] == "trt_empty_ecd"
    assert "deltaPct" in kpi and "onTarget" in kpi


# --- Carbon (C6) ------------------------------------------------------------
def test_carbon_rollup_synthetic(client):
    r = client.get("/api/carbon/rollup")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] == "SYNTHETIC"
    assert body["total_kg"] > 0
    assert "by_class" in body and "by_source" in body


def test_carbon_estimate_synthetic(client):
    r = client.post("/api/carbon/estimate",
                    json={"distance_km": 40, "payload_tonnes": 20,
                          "idle_minutes": 30, "vehicle_class": "HGV"})
    assert r.status_code == 200
    body = r.json()
    assert body["total_kg"] == pytest.approx(body["moving_kg"] + body["idle_kg"], rel=1e-6)


# --- Gate-data / Auto-LEO + Customs (C4, C5) --------------------------------
def test_leo_queue_synthetic(client):
    r = client.get("/api/gate-data/leo/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] == "SYNTHETIC"
    assert body["count"] >= 1
    assert "leo_ready" in body["results"][0]


def test_customs_flags_synthetic(client):
    r = client.get("/api/gate-data/customs/flags")
    assert r.status_code == 200
    body = r.json()
    # The seeded dataset deliberately includes mismatches, so flags must exist.
    assert body["count"] >= 1
    assert body["alerts"][0]["kind"] == "CUSTOMS_FLAG"


# --- Identity / face-recognition (C2) ---------------------------------------
def test_identity_gallery_synthetic(client):
    r = client.get("/api/identity/gallery")
    assert r.status_code == 200
    body = r.json()
    assert body["synthetic"] is True
    assert body["count"] >= 1
    assert "embedding" not in body["drivers"][0]  # raw templates never leave


def test_identity_verify_genuine_then_unknown(client):
    drv = client.get("/api/identity/gallery").json()["drivers"][0]["driver_id"]
    r = client.post("/api/identity/verify", json={"driver_id": drv, "simulate": "genuine"})
    assert r.json()["decision"] == "VERIFIED"
    r2 = client.post("/api/identity/verify",
                     json={"driver_id": "UNKNOWN-XYZ", "simulate": "unknown"})
    body2 = r2.json()
    assert body2["decision"] == "PROVISIONAL"
    assert body2["cure_window_h"] == 24


# --- Parking (C1) -----------------------------------------------------------
# The board is RDS-backed (no synthetic occupancy). Without a reachable DB the
# gateway degrades gracefully to source="unavailable" instead of 500ing; with
# real slot state it returns source="rds". Either way the invariant
# available == capacity - occupied must hold per facility.
def test_parking_availability_rds_backed(client):
    r = client.get("/api/parking/availability")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] in {"LIVE", "RDS_DIRECT", "UNAVAILABLE"}
    for f in body.get("facilities", []):
        assert f["available"] == f["capacity"] - f["occupied"]


def test_parking_summary_totals(client):
    r = client.get("/api/parking/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_path"] in {"LIVE", "RDS_DIRECT", "UNAVAILABLE"}
    assert body["available"] == body["capacity"] - body["occupied"]
