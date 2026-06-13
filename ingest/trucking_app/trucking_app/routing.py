"""Route acquisition with graceful provider fallback.

For each truck we need a driving route from its origin to its target gate (and
the reverse for the trip home). Three providers are tried in order:

  1. **OSRM** public demo (``router.project-osrm.org``) — primary.
  2. **HERE** Routing v8 — fallback, only if ``HERE_API_KEY`` is set.
  3. **Dead reckoning** — if both fail, synthesize a straight-line bearing route
     that snaps onto the shared NH-348 corridor polyline near the gate, so the
     position still evolves along a plausible corridor shape.

A route is a densified ``[(lat, lon), ...]`` polyline plus a ``duration_s``
estimate. Routes (origin->gate and gate->origin) are cached in a bounded LRU so
20k trucks don't hammer the public OSRM demo: many trucks share a gate and the
origins cluster, and the *shape* is what matters for the sim.

All network calls are short-timeout and fully fault-tolerant: a provider error
never propagates — it just falls through to the next provider, and the metrics
record which provider actually served the route.
"""
from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Tuple

import httpx

from jnpa_shared.corridor import WAYPOINTS, haversine_km
from jnpa_shared.logging import get_logger

from .config import TruckConfig
from .metrics import ROUTE_FETCH_SECONDS, ROUTES_FETCHED

log = get_logger("trucking_app.routing")

LatLon = Tuple[float, float]


@dataclass
class Route:
    """A driving route: a densified polyline + a duration estimate."""

    points: List[LatLon]          # ordered (lat, lon), origin first
    duration_s: float             # provider's duration, or dead-reckoned estimate
    provider: str                 # "osrm" | "here" | "deadreckon"

    @property
    def length_km(self) -> float:
        return sum(
            haversine_km(self.points[i], self.points[i + 1])
            for i in range(len(self.points) - 1)
        )


def _round_key(p: LatLon) -> Tuple[float, float]:
    """Quantise a coordinate to ~1.1 km so nearby origins share a cached route."""
    return (round(p[0], 2), round(p[1], 2))


class Router:
    """Async route fetcher with provider fallback + a bounded LRU cache."""

    def __init__(self, cfg: TruckConfig) -> None:
        self.cfg = cfg
        self._sem = asyncio.Semaphore(cfg.routing_max_concurrency)
        self._cache: "OrderedDict[tuple, Route]" = OrderedDict()
        self._client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        # One shared connection-pooled client for all route fetches.
        self._client = httpx.AsyncClient(
            timeout=self.cfg.osrm_timeout_s,
            headers={"User-Agent": "jnpa-uc3-truck-sim/0.1"},
            limits=httpx.Limits(max_connections=self.cfg.routing_max_concurrency),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def route(self, origin: LatLon, dest: LatLon) -> Route:
        """Return a route origin->dest, fetching/caching as needed."""
        key = (_round_key(origin), _round_key(dest))
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        route = await self._fetch(origin, dest)
        self._cache[key] = route
        self._cache.move_to_end(key)
        while len(self._cache) > self.cfg.route_cache_size:
            self._cache.popitem(last=False)
        return route

    async def duration_s(self, origin: LatLon, dest: LatLon) -> Optional[float]:
        """Current driving duration origin->dest for ETA (OSRM/HERE only).

        Returns ``None`` if no live provider answered — callers then fall back to
        a speed-based estimate. This is NOT cached: the whole point of ETA is a
        fresh duration reflecting current congestion.
        """
        async with self._sem:
            dur = await self._osrm_duration(origin, dest)
            if dur is None and self.cfg.here_api_key:
                dur = await self._here_duration(origin, dest)
        return dur

    # -- provider chain -----------------------------------------------------
    async def _fetch(self, origin: LatLon, dest: LatLon) -> Route:
        async with self._sem:
            for provider, coro in (
                ("osrm", self._osrm_route),
                ("here", self._here_route),
            ):
                if provider == "here" and not self.cfg.here_api_key:
                    continue
                try:
                    with ROUTE_FETCH_SECONDS.labels(provider).time():
                        route = await coro(origin, dest)
                    if route and len(route.points) >= 2:
                        ROUTES_FETCHED.labels(provider).inc()
                        return route
                except Exception as exc:  # noqa: BLE001 - any failure -> next provider
                    log.debug("route_provider_failed", provider=provider, error=str(exc))

        # Both live providers unavailable: dead reckon along the corridor.
        with ROUTE_FETCH_SECONDS.labels("deadreckon").time():
            route = self._dead_reckon(origin, dest)
        ROUTES_FETCHED.labels("deadreckon").inc()
        return route

    # -- OSRM ---------------------------------------------------------------
    async def _osrm_route(self, origin: LatLon, dest: LatLon) -> Optional[Route]:
        assert self._client is not None
        # OSRM expects lon,lat;lon,lat.
        coords = f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
        url = self.cfg.osrm_base_url.rstrip("/") + "/" + coords
        params = {"overview": "full", "geometries": "geojson"}
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            return None
        r0 = data["routes"][0]
        coords_ll = r0["geometry"]["coordinates"]  # [lon, lat] pairs
        points = [(c[1], c[0]) for c in coords_ll]
        return Route(points=points, duration_s=float(r0["duration"]), provider="osrm")

    async def _osrm_duration(self, origin: LatLon, dest: LatLon) -> Optional[float]:
        assert self._client is not None
        coords = f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}"
        url = self.cfg.osrm_base_url.rstrip("/") + "/" + coords
        try:
            resp = await self._client.get(url, params={"overview": "false"})
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == "Ok" and data.get("routes"):
                return float(data["routes"][0]["duration"])
        except Exception as exc:  # noqa: BLE001
            log.debug("osrm_duration_failed", error=str(exc))
        return None

    # -- HERE ---------------------------------------------------------------
    async def _here_route(self, origin: LatLon, dest: LatLon) -> Optional[Route]:
        assert self._client is not None
        params = {
            "transportMode": "truck",
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{dest[0]},{dest[1]}",
            "return": "polyline,summary",
            "apikey": self.cfg.here_api_key,
        }
        resp = await self._client.get(self.cfg.here_base_url, params=params)
        resp.raise_for_status()
        data = resp.json()
        routes = data.get("routes") or []
        if not routes:
            return None
        sections = routes[0].get("sections") or []
        points: List[LatLon] = []
        duration = 0.0
        for sec in sections:
            duration += float(sec.get("summary", {}).get("duration", 0.0))
            poly = sec.get("polyline")
            if poly:
                points.extend(_decode_flexible_polyline(poly))
        if len(points) < 2:
            return None
        return Route(points=points, duration_s=duration, provider="here")

    async def _here_duration(self, origin: LatLon, dest: LatLon) -> Optional[float]:
        route = None
        try:
            route = await self._here_route(origin, dest)
        except Exception as exc:  # noqa: BLE001
            log.debug("here_duration_failed", error=str(exc))
        return route.duration_s if route else None

    # -- dead reckoning -----------------------------------------------------
    def _dead_reckon(self, origin: LatLon, dest: LatLon) -> Route:
        """Straight-line origin -> nearest corridor entry -> snap down to dest.

        We bend the straight line through the closest NH-348 corridor waypoint to
        the destination gate, so even offline the truck visibly travels *down the
        corridor* into the port rather than cutting across the map. Points are
        densified to ~1 km spacing for smooth interpolation.
        """
        # Corridor waypoint nearest the destination (the gate end of NH-348).
        entry = min(WAYPOINTS, key=lambda w: haversine_km(w, dest))
        anchors: List[LatLon] = [origin, entry, dest]
        # Drop the entry anchor if it doesn't actually sit between origin & dest
        # (e.g. origin already on the corridor) to avoid a visible kink.
        if haversine_km(origin, entry) + haversine_km(entry, dest) > 1.4 * haversine_km(
            origin, dest
        ):
            anchors = [origin, dest]

        points = _densify(anchors, step_km=1.0)
        length = sum(
            haversine_km(points[i], points[i + 1]) for i in range(len(points) - 1)
        )
        # Duration estimate at the highway free-flow speed (spec default 55 km/h).
        duration = (length / max(1e-6, self.cfg.speed_highway_kmh)) * 3600.0
        return Route(points=points, duration_s=duration, provider="deadreckon")


def _densify(anchors: List[LatLon], step_km: float) -> List[LatLon]:
    """Interpolate intermediate points so consecutive points are ~step_km apart."""
    out: List[LatLon] = [anchors[0]]
    for a, b in zip(anchors, anchors[1:]):
        seg_km = haversine_km(a, b)
        n = max(1, int(seg_km / step_km))
        for k in range(1, n + 1):
            frac = k / n
            out.append((a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac))
    return out


# ---------------------------------------------------------------------------
# HERE flexible-polyline decoder (HERE encodes geometry in its own format).
# Compact port of the reference algorithm; lat/lng only (3rd dim ignored).
# ---------------------------------------------------------------------------
_DECODING_TABLE = [
    62, 63, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1,
    52, 53, 54, 55, 56, 57, 58, 59, 60, 61, -1, -1, -1, -1, -1, -1,
    -1, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
    15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, -1, -1, -1, -1, 63,
    -1, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
    41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51,
]


def _decode_flexible_polyline(encoded: str) -> List[LatLon]:
    try:
        return _decode_flexible_polyline_impl(encoded)
    except Exception:  # noqa: BLE001 - never let a decode error kill routing
        return []


def _decode_flexible_polyline_impl(encoded: str) -> List[LatLon]:
    def decode_unsigned(it):
        result = 0
        shift = 0
        for ch in it:
            val = _DECODING_TABLE[ord(ch) - 45]
            if val < 0:
                raise ValueError("invalid encoding char")
            result |= (val & 0x1F) << shift
            if (val & 0x20) == 0:
                return result, True
            shift += 5
        return result, False

    chars = iter(encoded)
    header_val, ok = decode_unsigned(chars)
    if not ok:
        return []
    precision = header_val & 0x0F
    third_dim = (header_val >> 4) & 0x07
    factor = 10 ** precision

    def to_signed(v: int) -> int:
        return ~(v >> 1) if (v & 1) else (v >> 1)

    points: List[LatLon] = []
    last_lat = 0
    last_lng = 0
    while True:
        lat_d, ok = decode_unsigned(chars)
        if not ok:
            break
        lng_d, ok = decode_unsigned(chars)
        if not ok:
            break
        last_lat += to_signed(lat_d)
        last_lng += to_signed(lng_d)
        if third_dim:  # consume and ignore the 3rd dimension delta
            _, ok = decode_unsigned(chars)
            if not ok:
                break
        points.append((last_lat / factor, last_lng / factor))
    return points
