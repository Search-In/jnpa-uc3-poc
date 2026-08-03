"""Traffic service — TomTom Flow + Incidents with graceful fallback.

The single entry point the /api/traffic/current router calls. Thin over
:class:`integrations.tomtom.TomTomClient` (HTTP) and
:class:`services.traffic.repository.TrafficRepository` (persistence), in the
same mould as services.weather.service: stateless apart from config.

Fallback chain (per data block, mirroring the weather service's vocabulary —
a provider outage must NEVER break the API):

    LIVE       -> fresh fetch (flow + incidents concurrently)
    CACHED     -> last good combined answer from Redis
                  (key jnpa:cache:tomtom:{lat}:{lon})
    DATABASE   -> the last persisted core.traffic_reading row for the
                  coordinate (the audit trail doubles as the third rung)
    SYNTHETIC  -> deterministic corridor conditions, clearly tagged

Response contract (source metadata always attached):
    status  LIVE      — every block fresh from TomTom
            DEGRADED  — at least one block served from a fallback rung
            OFFLINE   — nothing real available, everything synthetic
    source  TOMTOM | TOMTOM_CACHE | TOMTOM_DB | SYNTHETIC  (worst rung fired)

A fully-LIVE answer is written back to Redis + core.traffic_reading
(best-effort — an infra blip never fails the request being served).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from jnpa_shared import redis_io
from jnpa_shared.config import get_settings
from jnpa_shared.logging import get_logger

from integrations.tomtom import TomTomClient, TomTomError

from .repository import TrafficRepository

log = get_logger("services.traffic.service")

# Cache-key convention matches gateway/cache.py: jnpa:cache:{api}:{key}.
CACHE_PREFIX = "jnpa:cache:tomtom"
DEFAULT_CACHE_TTL_S = 120

# Decision-path rungs (per block) and the source label each one implies.
PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_DATABASE = "DATABASE"
PATH_SYNTHETIC = "SYNTHETIC"
_PATH_RANK = {PATH_LIVE: 0, PATH_CACHED: 1, PATH_DATABASE: 2, PATH_SYNTHETIC: 3}
_PATH_SOURCE = {PATH_LIVE: "TOMTOM", PATH_CACHED: "TOMTOM_CACHE",
                PATH_DATABASE: "TOMTOM_DB", PATH_SYNTHETIC: "SYNTHETIC"}

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# Bounding-box margin (degrees, ~2 km) added around the corridor endpoints for
# the incident search area.
BBOX_MARGIN_DEG = 0.02

# Default units for the normalised blocks (documentation for clients).
UNITS: Dict[str, str] = {
    "current_speed": "km/h", "free_flow_speed": "km/h",
    "current_travel_time": "s", "free_flow_travel_time": "s",
    "delay_seconds": "s", "delay": "s",
}


def cache_key(latitude: float, longitude: float) -> str:
    """Canonical Redis key, coordinate-bucketed at 3 dp (~110 m)."""
    return f"{CACHE_PREFIX}:{latitude:.3f}:{longitude:.3f}"


def corridor_bbox() -> Tuple[float, float, float, float]:
    """(min_lon, min_lat, max_lon, max_lat) covering the configured JNPA
    corridor (port <-> Karal Phata) plus a small margin — env-driven via the
    shared settings, never hardcoded here."""
    s = get_settings()
    lats = (s.port_lat, s.karal_lat)
    lons = (s.port_lon, s.karal_lon)
    return (min(lons) - BBOX_MARGIN_DEG, min(lats) - BBOX_MARGIN_DEG,
            max(lons) + BBOX_MARGIN_DEG, max(lats) + BBOX_MARGIN_DEG)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def synthetic_traffic() -> Dict[str, Any]:
    """Deterministic corridor traffic — the last-ditch rung, clearly tagged.
    Near-free-flow NH-348 conditions (ratio 0.86 -> LOW)."""
    return {
        "current_speed": 43.0, "free_flow_speed": 50.0,
        "current_travel_time": 540.0, "free_flow_travel_time": 465.0,
        "congestion_level": "LOW", "delay_seconds": 75.0,
        "road_closure": False, "confidence": None, "road_class": None,
        "synthetic": True,
    }


def synthetic_incidents() -> List[Dict[str, Any]]:
    """Deterministic incident floor — an empty list (no fabricated incidents)."""
    return []


# Module-level cache primitives (monkeypatchable in tests; best-effort like
# services.weather.service — a Redis outage must never fail the request).
async def _cache_put(key: str, value: Dict[str, Any], ttl: int) -> None:
    wrapped = {"cached_at": _now_iso(), "value": value}
    try:
        await redis_io.cache_set(key, wrapped, ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("traffic_cache_put_failed", key=key, error=str(exc))


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = await redis_io.cache_get(key)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("traffic_cache_get_failed", key=key, error=str(exc))
        return None
    if not isinstance(raw, dict) or "value" not in raw:
        return None
    return {"value": raw["value"], "cached_at": raw.get("cached_at"),
            "age_s": _age_seconds(raw.get("cached_at"))}


def _age_seconds(cached_at: Optional[str]) -> Optional[float]:
    if not cached_at:
        return None
    try:
        then = datetime.fromisoformat(cached_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round((datetime.now(tz=timezone.utc) - then).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


class TrafficService:
    """Fetch and normalise TomTom corridor traffic (flow + incidents) with the
    LIVE -> CACHED -> DATABASE -> SYNTHETIC fallback chain."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[TomTomClient] = None,
        repository: Optional[TrafficRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._client = client or TomTomClient()
        self._repo = repository or TrafficRepository(dsn)
        self.cache_ttl_s = cache_ttl_s

    @property
    def configured(self) -> bool:
        """True when the TomTom provider participates (API key present)."""
        return self._client.configured

    # ---------------------------------------------------------------- current
    async def current(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Current corridor traffic. Never raises for an upstream failure —
        it degrades through CACHED and DATABASE to SYNTHETIC and says so in
        the metadata."""
        key = cache_key(latitude, longitude)

        traffic: Optional[Dict[str, Any]] = None
        incidents: Optional[List[Dict[str, Any]]] = None
        traffic_path = incidents_path = PATH_LIVE

        # ------------------------------------------------ LIVE rung (TomTom)
        if self.configured:
            bbox = corridor_bbox()
            fres, ires = await asyncio.gather(
                self._client.fetch_flow(latitude, longitude),
                self._client.fetch_incidents(*bbox),
                return_exceptions=True,
            )
            if isinstance(fres, TomTomError):
                log.warning("traffic_flow_live_failed", lat=latitude, lon=longitude,
                            error=str(fres))
            elif isinstance(fres, BaseException):
                raise fres
            else:
                traffic = fres.normalize()
            if isinstance(ires, TomTomError):
                log.warning("traffic_incidents_live_failed", lat=latitude,
                            lon=longitude, error=str(ires))
            elif isinstance(ires, BaseException):
                raise ires
            else:
                incidents = ires.normalize()
        else:
            log.debug("traffic_provider_disabled")

        # ------------------------------------------------ CACHED rung (Redis)
        cached: Optional[Dict[str, Any]] = None
        cache_age_s: Optional[float] = None
        if traffic is None or incidents is None:
            cached = await _cache_get(key)
            if cached is not None:
                if traffic is None and cached["value"].get("traffic"):
                    traffic, traffic_path = cached["value"]["traffic"], PATH_CACHED
                if incidents is None and cached["value"].get("incidents") is not None:
                    incidents = cached["value"]["incidents"]
                    incidents_path = PATH_CACHED
                cache_age_s = cached.get("age_s")

        # -------------------------------------------- DATABASE rung (Postgres)
        if traffic is None or incidents is None:
            row = await self._db_fallback(latitude, longitude)
            if row is not None:
                if traffic is None and row["value"].get("traffic"):
                    traffic, traffic_path = row["value"]["traffic"], PATH_DATABASE
                if incidents is None and row["value"].get("incidents") is not None:
                    incidents = row["value"]["incidents"]
                    incidents_path = PATH_DATABASE
                if cache_age_s is None:
                    cache_age_s = row.get("age_s")

        # ---------------------------------------------- SYNTHETIC rung (floor)
        if traffic is None:
            traffic, traffic_path = synthetic_traffic(), PATH_SYNTHETIC
        if incidents is None:
            incidents, incidents_path = synthetic_incidents(), PATH_SYNTHETIC

        # ------------------------------------ write-back + persist (LIVE only)
        if traffic_path == PATH_LIVE and incidents_path == PATH_LIVE:
            value: Dict[str, Any] = {"traffic": traffic, "incidents": incidents}
            await _cache_put(key, value, self.cache_ttl_s)
            await self._persist(latitude, longitude, traffic, incidents)

        paths = [traffic_path, incidents_path]
        worst = max(paths, key=_PATH_RANK.__getitem__)
        if worst == PATH_LIVE:
            status = STATUS_LIVE
        elif all(p == PATH_SYNTHETIC for p in paths):
            status = STATUS_OFFLINE
        else:
            status = STATUS_DEGRADED

        return {
            "status": status,
            "source": _PATH_SOURCE[worst],
            "decision_path": worst,
            "location": {"latitude": latitude, "longitude": longitude},
            "traffic": traffic,
            "incidents": incidents,
            "incident_count": len(incidents),
            "sources": {"traffic": traffic_path, "incidents": incidents_path},
            "cache_age_s": cache_age_s,
            "units": UNITS,
            "timestamp": _now_iso(),
        }

    # ---------------------------------------------------------------- history
    async def readings(self, *, latitude: Optional[float] = None,
                       longitude: Optional[float] = None,
                       limit: int = 100, offset: int = 0) -> Tuple[list, int]:
        """Persisted reading history (newest first) + total count."""
        items = await self._repo.list_readings(latitude=latitude, longitude=longitude,
                                               limit=limit, offset=offset)
        total = await self._repo.count_readings(latitude=latitude, longitude=longitude)
        return items, total

    # ---------------------------------------------------------------- helpers
    async def _db_fallback(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        """Rebuild a cache-shaped value from the last persisted reading (rung 3)."""
        try:
            row = await self._repo.latest_reading(latitude, longitude)
        except Exception as exc:  # noqa: BLE001 - DB down => keep degrading
            log.warning("traffic_db_fallback_failed", error=str(exc))
            return None
        if row is None:
            return None
        payload = row.get("payload") or {}
        traffic = payload.get("traffic") or {
            "current_speed": _as_float(row.get("current_speed")),
            "free_flow_speed": _as_float(row.get("free_flow_speed")),
            "current_travel_time": None, "free_flow_travel_time": None,
            "congestion_level": row.get("congestion_level"),
            "delay_seconds": _as_float(row.get("delay_seconds")),
            "road_closure": False, "confidence": None, "road_class": None,
        }
        created_at = row.get("created_at")
        cached_at = created_at.isoformat() if isinstance(created_at, datetime) else None
        value: Dict[str, Any] = {"traffic": traffic}
        # incidents are only present in payloads persisted while LIVE.
        if payload.get("incidents") is not None:
            value["incidents"] = payload["incidents"]
        return {"value": value, "cached_at": cached_at, "age_s": _age_seconds(cached_at)}

    async def _persist(self, latitude: float, longitude: float,
                       traffic: Dict[str, Any],
                       incidents: List[Dict[str, Any]]) -> None:
        """Append the LIVE reading to core.traffic_reading (best-effort)."""
        try:
            await self._repo.insert_reading(
                latitude=latitude, longitude=longitude,
                current_speed=traffic.get("current_speed"),
                free_flow_speed=traffic.get("free_flow_speed"),
                congestion_level=traffic.get("congestion_level"),
                delay_seconds=traffic.get("delay_seconds"),
                source="TOMTOM",
                payload={"traffic": traffic, "incidents": incidents},
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("traffic_persist_failed", lat=latitude, lon=longitude,
                        error=str(exc))


def _as_float(value: Any) -> Optional[float]:
    """DB numerics surface as Decimal — JSON-safe floats out, None-safe."""
    return float(value) if value is not None else None


__all__ = ["TrafficService", "cache_key", "corridor_bbox",
           "synthetic_traffic", "synthetic_incidents",
           "STATUS_LIVE", "STATUS_DEGRADED", "STATUS_OFFLINE",
           "PATH_LIVE", "PATH_CACHED", "PATH_DATABASE", "PATH_SYNTHETIC", "UNITS"]
