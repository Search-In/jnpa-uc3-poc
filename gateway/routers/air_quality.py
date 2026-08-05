"""/api/air-quality — OpenAQ Air Quality Intelligence for the JNPA port.

ADDITIVE — same mould as gateway/routers/traffic.py's TomTom endpoints: a thin
router over :class:`services.air_quality.AirQualityService` →
integrations/openaq. No API key is required (an optional OPENAQ_API_KEY raises
the platform rate limit); everything is backend-only — the browser never talks
to api.openaq.org. A provider outage NEVER breaks this surface —
LIVE → CACHED (Redis) → DATABASE (core.air_quality_readings) → SYNTHETIC:

    GET /api/air-quality/current  -> normalised pollutant block for the port
    GET /api/air-quality/health   -> OpenAQ integration posture
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, Request

from jnpa_shared.config import get_settings

from ..metrics import REQUESTS
from ..logging import get_logger

from services.air_quality import AirQualityService

log = get_logger("gateway.air_quality")

router = APIRouter(prefix="/api/air-quality", tags=["air-quality"])

_service: Optional[AirQualityService] = None


def get_service(request: Request) -> AirQualityService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        # The OpenAQClient is entirely env-driven (OPENAQ_API_URL /
        # OPENAQ_TIMEOUT_S / OPENAQ_RETRIES / optional OPENAQ_API_KEY) —
        # no key gate, so no gateway-config coupling is needed.
        try:
            cache_ttl = int(os.environ.get("GATEWAY_CACHE_TTL_OPENAQ_S", "300"))
        except ValueError:
            cache_ttl = 300
        _service = AirQualityService(
            dsn=getattr(cfg, "postgres_dsn", None) or None,
            cache_ttl_s=cache_ttl,
        )
    return _service


def _default_coords(latitude: Optional[float], longitude: Optional[float]) -> tuple[float, float]:
    """Fall back to the configured JNPA port coordinates (env-driven, not hardcoded)."""
    s = get_settings()
    return (latitude if latitude is not None else s.port_lat,
            longitude if longitude is not None else s.port_lon)


# ------------------------------------------------------------------- current
@router.get("/current",
            summary="OpenAQ air quality around the JNPA port")
async def current_air_quality(
    latitude: Optional[float] = Query(None, ge=-90, le=90),
    longitude: Optional[float] = Query(None, ge=-180, le=180),
    svc: AirQualityService = Depends(get_service),
) -> Dict[str, Any]:
    lat, lon = _default_coords(latitude, longitude)
    result = await svc.current(lat, lon)
    REQUESTS.labels("air_quality", "ok" if result["status"] == "LIVE" else "error").inc()
    return result


# ------------------------------------------------------------------- health
@router.get("/health", summary="OpenAQ air-quality integration posture")
async def air_quality_health(svc: AirQualityService = Depends(get_service)) -> Dict[str, Any]:
    s = get_settings()
    client = svc._client  # noqa: SLF001 - posture only; the optional key is never returned
    return {
        "system": "AIR_QUALITY",
        "provider": "OPENAQ",
        "configured": svc.configured,
        "api_key_required": False,
        "api_key_present": bool(client.api_key),
        "base_url": client.base_url,
        "timeout_s": client.timeout_s,
        "retries": client.retries,
        "radius_m": client.radius_m,
        "cache_ttl_s": svc.cache_ttl_s,
        "default_location": {"latitude": s.port_lat, "longitude": s.port_lon},
    }
