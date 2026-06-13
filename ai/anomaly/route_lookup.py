"""Assigned-route lookup against the trucking-app control plane.

The route-deviation rule needs a truck's *assigned* route to compare against its
actual GPS path. The trucking-app simulator exposes that as
``GET /devices/{device_id}/route`` (added in Prompt 7), returning an ordered
``points: [[lat, lon], ...]`` polyline. This helper fetches and caches it.

Best-effort: a lookup failure (truck-sim down, unknown device, empty route)
returns ``None`` and the engine simply skips route-deviation for that track —
never crashes the detector. Routes change rarely (only on (re)binding), so we
cache them in-process with a short TTL to avoid hammering the control plane.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

import httpx

from jnpa_shared.logging import get_logger

from .config import AnomalyConfig

log = get_logger("anomaly.route_lookup")

LatLon = Tuple[float, float]
_CACHE_TTL_S = 60.0


class RouteCache:
    """Tiny in-process TTL cache of device_id -> assigned route polyline."""

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self._cache: Dict[str, Tuple[float, List[LatLon]]] = {}

    async def fetch_route(
        self, device_id: str, client: Optional[httpx.AsyncClient] = None
    ) -> Optional[List[LatLon]]:
        """Return the assigned route polyline for a device, or None.

        ``client`` lets the engine share one connection-pooled client across many
        lookups; if omitted a short-lived client is created per call.
        """
        now = time.monotonic()
        cached = self._cache.get(device_id)
        if cached is not None and (now - cached[0]) < _CACHE_TTL_S:
            return cached[1] or None

        url = f"{self.cfg.truck_api_url.rstrip('/')}/devices/{device_id}/route"
        try:
            if client is not None:
                resp = await client.get(url, timeout=self.cfg.truck_api_timeout_s)
            else:
                async with httpx.AsyncClient(timeout=self.cfg.truck_api_timeout_s) as c:
                    resp = await c.get(url)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - best-effort; skip the rule
            log.debug("route_fetch_failed", device_id=device_id, error=str(exc))
            return None

        pts_raw = data.get("points") or []
        route: List[LatLon] = [(float(p[0]), float(p[1])) for p in pts_raw if len(p) >= 2]
        self._cache[device_id] = (now, route)
        return route or None


__all__ = ["RouteCache"]
