"""Automatic congestion-alert tests (UC-3 audit Task 4).

Covers the pure detector (threshold + severity + ordering), the raise-and-fan-out
function with injected transports (no DB), and the awaited demo/e2e trigger
endpoint POST /api/traffic/congestion-scan.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from starlette.testclient import TestClient  # noqa: E402

from services import congestion_alert  # noqa: E402


@pytest.fixture()
def client():
    from gateway.main import app

    with TestClient(app) as c:
        yield c


def test_detect_congested_threshold_and_severity():
    preds = {"SEG-00": 0.95, "SEG-01": 0.79, "SEG-02": 0.82, "SEG-03": "nan"}
    out = congestion_alert.detect_congested(preds, 0.80)
    # 0.79 is below threshold; "nan"->float('nan') is not >= threshold; sorted desc.
    assert [a.segment_id for a in out] == ["SEG-00", "SEG-02"]
    assert out[0].severity == "CRITICAL"  # >= 0.90
    assert out[1].severity == "HIGH"      # >= 0.80
    assert out[0].payload()["type"] == "TRAFFIC_CONGESTION"


def test_detect_congested_none_below_threshold():
    # The deterministic synthetic fallback (0.05..0.34) must never trip an alert.
    preds = {f"SEG-{i:02d}": 0.05 + (i % 30) / 100.0 for i in range(13)}
    assert congestion_alert.detect_congested(preds, 0.80) == []


def test_raise_broadcasts_and_dispatches_with_real_status():
    frames = []
    pushes = []

    async def broadcast(type_, payload):
        frames.append((type_, payload))

    async def dispatch(device_id, advisory):
        pushes.append((device_id, advisory))
        return True  # delivered

    created = asyncio.run(
        congestion_alert.raise_congestion_alerts(
            predictions={"SEG-05": 0.91},
            threshold=0.80,
            dsn=None,  # no DB: persistence skipped, fan-out still verified
            broadcast=broadcast,
            dispatch=dispatch,
            device_targets=["TRK-000009"],
            bucket="2026-07-13T15",
        )
    )
    assert len(created) == 1
    assert created[0]["type"] == "TRAFFIC_CONGESTION"
    assert created[0]["severity"] == "CRITICAL"
    assert created[0]["recommended_action"]  # human-readable action present
    assert frames and frames[0][0] == "alert"
    assert pushes and pushes[0][0] == "TRK-000009"
    assert pushes[0][1]["truck_id"] == "TRK-000009"


def test_raise_returns_empty_when_below_threshold():
    created = asyncio.run(
        congestion_alert.raise_congestion_alerts(
            predictions={"SEG-01": 0.4}, threshold=0.80, dsn=None, bucket="B",
        )
    )
    assert created == []


def test_congestion_scan_endpoint(client):
    r = client.post(
        "/api/traffic/congestion-scan",
        json={"predictions": {"SEG-01": 0.91, "SEG-02": 0.5}, "threshold": 0.80},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["threshold"] == 0.80
    assert b["count"] >= 1
    kinds = {c["type"] for c in b["created"]}
    assert kinds == {"TRAFFIC_CONGESTION"}
