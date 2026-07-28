"""/api/weather — Open-Meteo Weather + Marine + OpenWeatherMap conditions for
the port area.

A thin router over :class:`services.weather.WeatherService` (service → OpenMeteo
+ OpenWeather clients + raw-SQL WeatherRepository), in the same mould as
gateway/routers/shipping_lines.py. The external seams are integrations/openmeteo
(free public APIs — no key) and integrations/openweather (key-gated via
OPENWEATHER_API_KEY, backend-only, never exposed to the browser); a provider
outage NEVER breaks this surface: the service degrades LIVE → CACHED (Redis,
then the last core.weather_reading row) → SYNTHETIC and says so via
status/source metadata — Open-Meteo data still returns when OpenWeather is down.

    GET /api/weather/current   -> combined weather + marine conditions
    GET /api/weather/readings  -> persisted reading history (filter + page)
    GET /api/weather/health    -> endpoint config / posture

Coordinates default to the configured JNPA port location (jnpa_shared settings
``port_lat`` / ``port_lon`` — env-overridable, never hardcoded here) and may be
overridden per request. No RBAC policy entry: weather is operational data,
visible to any authenticated stakeholder (same posture as traffic / kpi).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel

from jnpa_shared.config import get_settings

from services.weather import WeatherService

from ..metrics import REQUESTS

router = APIRouter(prefix="/api/weather", tags=["weather"])

_service: Optional[WeatherService] = None


def get_service(request: Request) -> WeatherService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        from integrations.openmeteo import OpenMeteoClient
        from integrations.openweather import OpenWeatherClient

        _service = WeatherService(
            dsn=getattr(cfg, "postgres_dsn", None) or None,
            client=OpenMeteoClient(
                weather_url=getattr(cfg, "open_meteo_weather_url", "") or None,
                marine_url=getattr(cfg, "open_meteo_marine_url", "") or None,
            ),
            # Key empty -> provider disabled; the openweather block is null and
            # the surface behaves exactly as the Open-Meteo-only build.
            openweather_client=OpenWeatherClient(
                api_key=getattr(cfg, "openweather_api_key", None),
                url=getattr(cfg, "openweather_url", "") or None,
            ),
            cache_ttl_s=getattr(cfg, "cache_ttl_weather_s", None) or 600,
        )
    return _service


# --------------------------------------------------------------------- DTOs
class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _default_coords(latitude: Optional[float], longitude: Optional[float]) -> tuple[float, float]:
    """Fall back to the configured JNPA port coordinates (env-driven, not hardcoded)."""
    s = get_settings()
    return (latitude if latitude is not None else s.port_lat,
            longitude if longitude is not None else s.port_lon)


# ------------------------------------------------------------------- current
@router.get("/current",
            summary="Combined Open-Meteo weather + marine + OpenWeatherMap conditions")
async def current_conditions(
    latitude: Optional[float] = Query(None, ge=-90, le=90),
    longitude: Optional[float] = Query(None, ge=-180, le=180),
    forecast_hours: int = Query(0, ge=0, le=48,
                                description="include N hours of hourly forecast (0 = off)"),
    svc: WeatherService = Depends(get_service),
) -> Dict[str, Any]:
    lat, lon = _default_coords(latitude, longitude)
    result = await svc.current(lat, lon, forecast_hours=forecast_hours)
    REQUESTS.labels("weather", "ok" if result["status"] == "LIVE" else "error").inc()
    return result


# ------------------------------------------------------------------ history
@router.get("/readings", response_model=Page, summary="Persisted weather-reading history")
async def list_readings(
    response: Response,
    latitude: Optional[float] = Query(None, ge=-90, le=90),
    longitude: Optional[float] = Query(None, ge=-180, le=180),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    svc: WeatherService = Depends(get_service),
) -> Page:
    items, total = await svc.readings(latitude=latitude, longitude=longitude,
                                      limit=limit, offset=offset)
    response.headers["X-Total-Count"] = str(total)
    return Page(items=items, total=total, limit=limit, offset=offset, count=len(items))


# ------------------------------------------------------------------- health
@router.get("/health", summary="Weather integration posture (Open-Meteo + OpenWeatherMap)")
async def weather_health(svc: WeatherService = Depends(get_service)) -> Dict[str, Any]:
    s = get_settings()
    client = svc._client  # noqa: SLF001 - posture surface for the health panel
    ow = svc._ow_client  # noqa: SLF001 - posture only; the key itself is never returned
    providers = ["OPEN_METEO"] + (["OPENWEATHER"] if ow.configured else [])
    return {
        "system": "WEATHER",
        "provider": " + ".join(providers),
        "providers": providers,
        "configured": True,          # Open-Meteo is public — the surface always works
        "api_key_required": False,
        "weather_url": client.weather_url,
        "marine_url": client.marine_url,
        "timeout_s": client.timeout_s,
        "retries": client.retries,
        # OpenWeatherMap posture — key-gated enrichment; the key is NEVER echoed.
        "openweather": {
            "configured": ow.configured,
            "api_key_required": True,
            "url": ow.url,
            "timeout_s": ow.timeout_s,
            "retries": ow.retries,
        },
        "cache_ttl_s": svc.cache_ttl_s,
        "default_location": {"latitude": s.port_lat, "longitude": s.port_lon},
    }
