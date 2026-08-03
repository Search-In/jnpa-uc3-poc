"""HERE integration tests (no network) — Traffic Flow v7 adapter + routing hygiene.

Covers (same httpx.MockTransport pattern as tests/test_tomtom.py):
  * speed unit conversion — v7 ``currentFlow.speed`` is metres/second
    (API reference: "The average speed (in meters per second), capped by the
    speed limit"), converted to km/h with * 3.6
  * ``speedUncapped`` fallback when ``speed`` is absent
  * ``jamFactor`` clamped to HERE's 0..10 scale
  * malformed / empty payload tolerance -> None (cascade drops to next source)
  * transport + HTTP errors -> None, never an exception (the SourceManager
    cascade contract), with the API key redacted from anything logged
  * keyless mode -> deterministic synthetic reading tagged "here"
  * one pooled httpx.AsyncClient reused across calls (+ aclose)
  * trucking_app Router._redact strips the key from provider-error log text
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT / "ai"),
          str(REPO_ROOT / "ingest" / "trucking_app")):
    if p not in sys.path:
        sys.path.insert(0, p)

from congestion.graph import SegmentMeta  # noqa: E402
from congestion.sources.here import HereSource  # noqa: E402
from trucking_app.config import TruckConfig  # noqa: E402
from trucking_app.routing import Router  # noqa: E402

KEY = "TEST-HERE-KEY-12345"


def _run(coro):
    return asyncio.run(coro)


def _seg() -> SegmentMeta:
    return SegmentMeta(id="SEG-03", index=3, length_km=1.2, lane_count=2,
                       signalised=False, lat=18.86, lon=73.01)


def _flow_payload(current_flow: dict) -> dict:
    """A representative v7 /flow answer (one result, shape referencing)."""
    return {
        "sourceUpdated": "2026-07-30T09:00:00Z",
        "results": [{
            "location": {"description": "NH-348", "length": 1200.0,
                         "shape": {"links": []}},
            "currentFlow": current_flow,
        }],
    }


def _source_with(payload=None, status=200, exc: Exception | None = None) -> HereSource:
    def handler(request: httpx.Request) -> httpx.Response:
        # The real request URL carries the key — exactly the leak surface the
        # adapter must contain.
        assert f"apiKey={KEY}" in str(request.url)
        if exc is not None:
            raise exc
        return httpx.Response(status, json=payload if payload is not None else {})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HereSource(api_key=KEY, http_client=client)


# ------------------------------------------------------------ unit conversion
def test_speed_converted_from_metres_per_second():
    # 13.61 m/s is a realistic capped urban speed (~49 km/h).
    src = _source_with(_flow_payload(
        {"speed": 13.61, "speedUncapped": 13.89, "freeFlow": 13.89,
         "jamFactor": 1.9, "confidence": 0.98}))
    r = _run(src.get_segment_speed(_seg()))
    assert r is not None
    assert r.speed_kmh == pytest.approx(13.61 * 3.6, abs=0.01)  # 49.0
    assert r.jam_factor == 1.9
    assert r.source == "here"
    assert r.stale is False


def test_speeduncapped_fallback_when_speed_missing():
    src = _source_with(_flow_payload({"speedUncapped": 13.89, "jamFactor": 0.0}))
    r = _run(src.get_segment_speed(_seg()))
    assert r is not None
    assert r.speed_kmh == pytest.approx(13.89 * 3.6, abs=0.01)  # 50.0


def test_jam_factor_clamped_to_here_scale():
    over = _source_with(_flow_payload({"speed": 5.0, "jamFactor": 12.5}))
    under = _source_with(_flow_payload({"speed": 5.0, "jamFactor": -3.0}))
    assert _run(over.get_segment_speed(_seg())).jam_factor == 10.0
    assert _run(under.get_segment_speed(_seg())).jam_factor == 0.0


# ------------------------------------------------------------------ tolerance
@pytest.mark.parametrize("payload", [
    {},                                            # no results at all
    {"results": []},                               # empty results
    _flow_payload({}),                             # currentFlow with no speeds
    _flow_payload({"speed": None}),                # explicit null speed
    {"results": [{"location": {}}]},               # result without currentFlow
])
def test_malformed_payload_returns_none(payload):
    src = _source_with(payload)
    assert _run(src.get_segment_speed(_seg())) is None


# ------------------------------------------------- failures stay inside + redact
def test_http_error_returns_none_not_exception():
    # 429/5xx used to escape as HTTPStatusError (whose message embeds the
    # apiKey-bearing URL); the adapter must contain it and answer None.
    for status in (401, 429, 500):
        src = _source_with(payload={"error": "x"}, status=status)
        assert _run(src.get_segment_speed(_seg())) is None


def test_transport_error_returns_none_not_exception():
    src = _source_with(exc=httpx.ConnectError(
        f"boom https://data.traffic.hereapi.com/v7/flow?apiKey={KEY}"))
    assert _run(src.get_segment_speed(_seg())) is None


def test_redact_strips_key_everywhere():
    src = HereSource(api_key=KEY)
    leaky = f"Client error '401' for url 'https://data.traffic.hereapi.com/v7/flow?apiKey={KEY}&in=circle'"
    assert KEY not in src._redact(leaky)
    assert "***" in src._redact(leaky)
    # Keyless adapter: redaction is a no-op, never a crash.
    assert HereSource()._redact("plain text") == "plain text"


# --------------------------------------------------------------- keyless mode
def test_keyless_returns_synthetic_tagged_here():
    src = HereSource(api_key="")
    r = _run(src.get_segment_speed(_seg()))
    assert r is not None
    assert r.source == "here"
    assert 0.0 <= r.jam_factor <= 10.0
    assert r.speed_kmh > 0.0


# ------------------------------------------------------------- pooled client
def test_pooled_client_is_reused_and_closable():
    src = HereSource(api_key=KEY)
    first = src._http()
    assert src._http() is first          # one shared client, not per-request
    _run(src.aclose())
    assert first.is_closed
    replacement = src._http()            # lazily replaced after close
    assert replacement is not first
    _run(src.aclose())


def test_injected_client_is_not_closed_by_adapter():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={})))
    src = HereSource(api_key=KEY, http_client=client)
    _run(src.aclose())                   # adapter must not close a borrowed client
    assert not client.is_closed
    _run(client.aclose())


# ---------------------------------------------------------------- timeouts
def test_timeout_returns_none_not_exception():
    src = _source_with(exc=httpx.ReadTimeout("timed out"))
    assert _run(src.get_segment_speed(_seg())) is None


# ------------------------------------------------- flexible-polyline decoder
# Official flexpolyline documentation example (version 1, precision 5, 2D).
_REF_POLYLINE = "BFoz5xJ67i1B1B7PzIhaxL7Y"
_REF_POINTS = [(50.10228, 8.69821), (50.10201, 8.69567),
               (50.10063, 8.69150), (50.09878, 8.68752)]


def test_decoder_matches_reference_vector():
    from trucking_app.routing import _decode_flexible_polyline
    pts = _decode_flexible_polyline(_REF_POLYLINE)
    assert len(pts) == len(_REF_POINTS)
    for (lat, lon), (elat, elon) in zip(pts, _REF_POINTS):
        assert lat == pytest.approx(elat, abs=1e-4)
        assert lon == pytest.approx(elon, abs=1e-4)


def test_decoder_tolerates_garbage():
    from trucking_app.routing import _decode_flexible_polyline
    assert _decode_flexible_polyline("") == []
    assert _decode_flexible_polyline("!!not/polyline??") == []
    # Wrong version byte -> refused, not garbage points.
    assert _decode_flexible_polyline("CFoz5xJ67i1B1B7PzIhaxL7Y") == []


# --------------------------------------------------------- Routing v8 parse
def _routing_body() -> dict:
    return {"routes": [{"id": "r1", "sections": [{
        "id": "s1", "type": "vehicle",
        "summary": {"duration": 2454, "length": 20686},
        "polyline": _REF_POLYLINE,
    }]}]}


def test_here_route_parses_v8_body():
    cfg = TruckConfig(here_api_key=KEY)
    router = Router(cfg)

    def handler(request: httpx.Request) -> httpx.Response:
        assert f"apikey={KEY}" in str(request.url)
        return httpx.Response(200, json=_routing_body())

    router._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        route = _run(router._here_route((50.10, 8.70), (50.09, 8.69)))
        assert route is not None
        assert route.provider == "here"
        assert len(route.points) == 4
        assert route.duration_s == pytest.approx(2454.0)
        # _here_duration rides the same parse and must not raise.
        assert _run(router._here_duration((50.10, 8.70), (50.09, 8.69))) == pytest.approx(2454.0)
    finally:
        _run(router._client.aclose())


def test_here_route_empty_routes_returns_none():
    router = Router(TruckConfig(here_api_key=KEY))
    router._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"routes": []})))
    try:
        assert _run(router._here_route((1.0, 2.0), (3.0, 4.0))) is None
    finally:
        _run(router._client.aclose())


# ------------------------------------------------------- cascade activation
class _FakeRedis:
    """Cache-miss Redis stand-in (same spirit as tests/test_congestion.py)."""
    async def cache_get(self, key):
        return None
    async def cache_set(self, key, value, ttl=None):
        return None
    def get_client(self):
        raise RuntimeError("no redis in unit tests")


def _manager(monkeypatch, **keys):
    from congestion.config import CongestionConfig
    from congestion.sources import manager as mgr_mod
    monkeypatch.setattr(mgr_mod, "redis_io", _FakeRedis())
    cfg = CongestionConfig(
        google_maps_api_key=keys.get("google", ""),
        here_api_key=keys.get("here", ""),
        tomtom_api_key=keys.get("tomtom", ""),
    )
    return mgr_mod.SourceManager(cfg)


def test_cascade_activates_here_when_only_its_key_is_set(monkeypatch):
    from congestion.sources.base import SpeedReading
    mgr = _manager(monkeypatch, here=KEY)
    # Configured providers must outrank keyless synthetic-answering ones.
    assert [s.name for s in mgr.sources] == ["here", "google", "tomtom"]

    async def fake_fetch(seg):
        return SpeedReading(segment_id=seg.id, speed_kmh=42.0, jam_factor=3.0,
                            source="here", ts="2026-07-30T00:00:00+00:00")

    monkeypatch.setattr(mgr.sources[0], "_fetch", fake_fetch)
    reading = _run(mgr.get(_seg()))
    assert reading.source == "here"
    assert reading.speed_kmh == 42.0


def test_cascade_falls_past_here_on_failure(monkeypatch):
    mgr = _manager(monkeypatch, here=KEY)

    async def dead_fetch(seg):
        return None   # e.g. no coverage / HTTP failure already contained

    monkeypatch.setattr(mgr.sources[0], "_fetch", dead_fetch)
    reading = _run(mgr.get(_seg()))
    # Next rung is a keyless provider -> synthetic reading, cascade never dies.
    assert reading is not None
    assert reading.source in ("google", "tomtom")


def test_cascade_order_unchanged_when_all_keyless(monkeypatch):
    mgr = _manager(monkeypatch)
    assert [s.name for s in mgr.sources] == ["google", "here", "tomtom"]
    reading = _run(mgr.get(_seg()))
    assert reading.source == "google"   # offline behaviour preserved


# ------------------------------------------------------- trucking_app routing
def test_router_redacts_here_key_in_error_text():
    router = Router(TruckConfig(here_api_key=KEY))
    leaky = f"Server error '503' for url 'https://router.hereapi.com/v8/routes?apikey={KEY}&origin=1,2'"
    redacted = router._redact(leaky)
    assert KEY not in redacted
    assert "***" in redacted
    # No key configured -> pass-through, never a crash.
    assert Router(TruckConfig())._redact("x") == "x"
