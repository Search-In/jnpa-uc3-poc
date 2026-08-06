"""GET /api/trucks fleet list — the Driver-Advisory latency fix.

Root cause pinned here: the Advisory queue blocked on the PRIMARY truck-sim
probe with a 12 s timeout, and a state-filtered query had NO rung between LIVE
and empty — a sim outage meant 12 s of spinner followed by a blank table.

The fix, asserted by these tests:
  * the fleet-LIST probe fails fast (connect 1.5 s / total 4 s) while the
    single-device probe and the reroute POST keep their 12 s budget;
  * a fresh in-process memo (≤3 s) answers without touching the sim at all;
  * on sim failure the last GOOD payload is served STALE — marked degraded,
    decision_path=CACHED, with its age — instead of an empty table;
  * past the memo the ladder is unchanged (filtered -> empty+hint, unfiltered ->
    RDS tail -> check-ins), so the existing fallback architecture is intact.

No server, no DB: handlers are called directly with the gateway state stubbed,
per this repo's router-test idiom.
"""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional

import httpx
import pytest

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from gateway.fallback import TruckPath  # noqa: E402
from gateway.routers import trucks as T  # noqa: E402


class _Resp:
    def __init__(self, status_code: int = 200, body: Optional[dict] = None):
        self.status_code = status_code
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _Http:
    """Stub httpx client: scripted response or exception, records timeouts."""

    def __init__(self, result: Any):
        self.result = result
        self.calls: list[dict] = []

    async def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _gw(http_result: Any) -> SimpleNamespace:
    async def record_decision(**kw):
        decisions.append(kw)

    decisions: list[dict] = []
    gw = SimpleNamespace(
        cfg=SimpleNamespace(truck_api_url="http://truck-sim:9000", postgres_dsn=None),
        http=_Http(http_result),
        record_decision=record_decision,
        decisions_log=decisions,
    )
    return gw


LIVE_BODY = {"count": 2, "devices": [{"device_id": "TRK-000001"}, {"device_id": "TRK-000002"}]}


@pytest.fixture(autouse=True)
def _clean_cache():
    T._LIST_CACHE.clear()
    yield
    T._LIST_CACHE.clear()


def _list(gw, state="AT_GATE_QUEUE", limit=500) -> Dict[str, Any]:
    return asyncio.run(T.list_trucks(state=state, limit=limit, gw=gw))


# ------------------------------------------------------------------ fast fail
def test_list_probe_uses_the_fast_budget_not_the_12s_one():
    """The page-blocking call must fail in seconds. The 12 s budget survives
    ONLY on the paths that are not page-blocking."""
    gw = _gw(_Resp(200, LIVE_BODY))
    _list(gw)
    assert gw.http.calls[0]["timeout"] is T.TRUCK_LIST_TIMEOUT
    assert T.TRUCK_LIST_TIMEOUT.connect == 1.5
    assert T.TRUCK_LIST_TIMEOUT.read == 4.0
    assert T.TRUCK_UPSTREAM_TIMEOUT_S == 12.0  # single-device probe + reroute POST


# ------------------------------------------------------------------ live path
def test_live_response_is_served_and_memoised():
    gw = _gw(_Resp(200, dict(LIVE_BODY)))
    body = _list(gw)
    assert body["decision_path"] == TruckPath.PRIMARY.value
    assert body["degraded"] is False
    assert T._LIST_CACHE  # memo recorded for the CACHED rung


def test_fresh_memo_short_circuits_the_sim_probe():
    """Every dashboard surface polls this endpoint; within the fresh window the
    memo answers and the sim sees ONE probe, not one per consumer."""
    gw = _gw(_Resp(200, dict(LIVE_BODY)))
    _list(gw)
    _list(gw)
    _list(gw)
    assert len(gw.http.calls) == 1


# ------------------------------------------------------------------ stale serve
def test_sim_outage_serves_last_good_payload_marked_cached():
    """THE ADVISORY FIX: a state-filtered query used to degrade to an empty
    table on sim failure. Now the last good queue is served, honestly marked."""
    good = _gw(_Resp(200, dict(LIVE_BODY)))
    _list(good)
    # age the memo past FRESH but inside STALE, then kill the sim
    key, (ts, body) = next(iter(T._LIST_CACHE.items()))
    T._LIST_CACHE[key] = (ts - (T.LIST_CACHE_FRESH_S + 1), body)

    dead = _gw(httpx.ConnectError("connection refused"))
    out = _list(dead)
    assert out["degraded"] is True
    assert out["decision_path"] == TruckPath.CACHED.value
    assert out["cache_age_s"] >= T.LIST_CACHE_FRESH_S
    assert [d["device_id"] for d in out["devices"]] == ["TRK-000001", "TRK-000002"]


def test_stale_memo_expires_and_the_old_ladder_returns():
    """Past the stale window the behaviour is byte-compatible with before:
    filtered -> empty + hint (state_filter_supported False)."""
    good = _gw(_Resp(200, dict(LIVE_BODY)))
    _list(good)
    key, (ts, body) = next(iter(T._LIST_CACHE.items()))
    T._LIST_CACHE[key] = (ts - (T.LIST_CACHE_STALE_S + 1), body)

    dead = _gw(httpx.ConnectError("connection refused"))
    out = _list(dead)
    assert out["devices"] == []
    assert out["degraded"] is True
    assert out["state_filter_supported"] is False
    assert "hint" in out


def test_memo_is_scoped_per_state_and_limit():
    """An AT_GATE_QUEUE memo must never answer an EN_ROUTE query."""
    gw = _gw(_Resp(200, dict(LIVE_BODY)))
    _list(gw, state="AT_GATE_QUEUE")
    dead = _gw(httpx.ConnectError("connection refused"))
    out = _list(dead, state="EN_ROUTE_TO_PORT")
    assert out["devices"] == []  # no cross-key bleed


# ------------------------------------------------------------------ chain intact
def test_unfiltered_query_still_reaches_the_rds_rung(monkeypatch):
    """The CACHED rung slots BETWEEN live and the RDS tail — with no memo, an
    unfiltered query on a dead sim must still fall through to SECONDARY."""
    async def fake_rds(state, limit):
        return [{"device_id": "TRK-000009", "source": "rds-telemetry"}]

    monkeypatch.setattr(T, "_list_secondary_rds", fake_rds)
    dead = _gw(httpx.ConnectError("connection refused"))
    out = _list(dead, state=None)
    assert out["decision_path"] == TruckPath.SECONDARY.value
    assert out["devices"][0]["device_id"] == "TRK-000009"


# ------------------------------------------------------------------ perf budget
def test_worst_case_first_paint_budget_is_seconds_not_twelve():
    """The whole point, stated as arithmetic: cold cache + dead sim now costs at
    most the connect budget (1.5 s unreachable / 4 s total), not 12 s."""
    assert T.TRUCK_LIST_TIMEOUT.connect <= 2.0
    assert T.TRUCK_LIST_TIMEOUT.read <= 5.0


def test_fresh_memo_answers_in_microseconds():
    gw = _gw(_Resp(200, dict(LIVE_BODY)))
    _list(gw)
    t0 = time.perf_counter()
    _list(gw)
    assert (time.perf_counter() - t0) < 0.05  # memo hit — no I/O at all
