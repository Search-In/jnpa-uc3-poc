"""Notification-pipeline end-to-end verification (UC-3 audit Task 1).

Traces the fan-out seam without a live DB:

    dispatch() -> WebSocket + WebPush + FCM   (per-channel result, no faked delivery)
    GET /api/notifications/health             -> {websocket, webpush, fcm, sms}
    GET /api/notifications/recent             -> durable delivery trail (empty w/o DB)

The dispatcher legs are stubbed to *observe* that each transport is invoked and
that the returned DispatchResult reflects the real per-channel outcome (True vs
False) — the audit's "do not fake successful delivery" requirement.
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


class _FakeWs:
    def __init__(self) -> None:
        self.frames = []

    async def broadcast(self, type_, payload):
        self.frames.append((type_, payload))


class _FakeGw:
    def __init__(self) -> None:
        self.ws = _FakeWs()


@pytest.fixture()
def client():
    from gateway.main import app

    with TestClient(app) as c:
        yield c


def test_dispatch_fans_out_over_all_transports(monkeypatch):
    """dispatch() attempts WS + WebPush + FCM and reports each leg's real result."""
    from gateway import notifications
    from gateway.routers import push

    calls = {"webpush": 0, "fcm": 0}

    async def fake_deliver(gw, device_id, payload):
        calls["webpush"] += 1
        return True  # WebPush succeeds

    async def fake_fcm(gw, device_id, payload):
        calls["fcm"] += 1
        return False  # FCM has no token -> not delivered

    monkeypatch.setattr(push, "deliver", fake_deliver)
    monkeypatch.setattr(push, "deliver_fcm", fake_fcm)

    gw = _FakeGw()
    res = asyncio.run(
        notifications.dispatch(gw, "TRK-000001",
                               {"type": "reroute", "body": "Gate closed"},
                               ws_type="reroute")
    )
    # Every transport attempted; the result is the true per-channel outcome.
    assert res.ws is True and res.webpush is True and res.fcm is False
    assert calls == {"webpush": 1, "fcm": 1}
    assert gw.ws.frames and gw.ws.frames[0][0] == "reroute"


def test_dispatch_alert_noops_without_device():
    from gateway import notifications

    gw = _FakeGw()
    res = asyncio.run(
        notifications.dispatch_alert(gw, None, kind="TRAFFIC_CONGESTION",
                                     title="x", body="y")
    )
    assert res is None
    assert gw.ws.frames == []  # nothing pushed when there is no bound driver


def test_notifications_health_reports_four_transports(client):
    r = client.get("/api/notifications/health")
    assert r.status_code == 200, r.text
    b = r.json()
    # The four booleans the audit asked for are present and typed.
    for k in ("websocket", "webpush", "fcm", "sms"):
        assert k in b and isinstance(b[k], bool)
    # WS needs no config -> always up; the others are env-gated (off in test env).
    assert b["websocket"] is True
    assert b["webpush"] is False  # VAPID keys unset in the test environment
    assert "detail" in b and "webpush" in b["detail"]


def test_notifications_recent_is_safe_without_db(client):
    r = client.get("/api/notifications/recent")
    assert r.status_code == 200
    assert r.json() == {"records": [], "count": 0}
