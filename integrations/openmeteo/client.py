"""Open-Meteo HTTP client — the ONLY layer that talks to the Open-Meteo APIs.

No account, no API key: both endpoints are public. Everything is env-driven —
NO hardcoded vendor URL in business code (mirrors services.fastag.client):

    OPEN_METEO_WEATHER_URL   (default https://api.open-meteo.com/v1/forecast)
    OPEN_METEO_MARINE_URL    (default https://marine-api.open-meteo.com/v1/marine)
    OPEN_METEO_TIMEOUT_S     per-attempt budget       (default 5.0)
    OPEN_METEO_RETRIES       retries AFTER the first try (default 2)

Failure contract: every failure surfaces as a typed
:class:`~integrations.openmeteo.exceptions.OpenMeteoError` subclass. Timeouts,
network errors and 5xx are retried with exponential backoff; 4xx (bad
coordinates / params) fail fast — retrying a rejected request cannot help.
200 bodies are validated through the pydantic schemas before anything
downstream sees them.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    OpenMeteoError,
    OpenMeteoHTTPError,
    OpenMeteoInvalidResponse,
    OpenMeteoTimeout,
    OpenMeteoUnavailable,
)
from .schemas import MarineResponse, WeatherResponse

log = get_logger("integrations.openmeteo.client")

DEFAULT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"

# Variables requested per endpoint. Visibility exists ONLY as an hourly series
# (the schemas read it at the current hour); everything else is asked for in
# both blocks so a provider-side gap in one still resolves from the other.
_WEATHER_CURRENT_VARS = ("temperature_2m,wind_speed_10m,wind_direction_10m,"
                         "wind_gusts_10m,precipitation,weather_code")
_WEATHER_HOURLY_VARS = _WEATHER_CURRENT_VARS + ",visibility"
_MARINE_VARS = "wave_height,wave_period,swell_wave_height,sea_level_height_msl"


def _as_float(value: Optional[str], default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


class OpenMeteoClient:
    """Async client for the Open-Meteo Weather + Marine APIs.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :func:`gateway.integrations.call`.
    """

    def __init__(
        self,
        weather_url: Optional[str] = None,
        marine_url: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.weather_url = (weather_url or env.get("OPEN_METEO_WEATHER_URL", "").strip()
                            or DEFAULT_WEATHER_URL)
        self.marine_url = (marine_url or env.get("OPEN_METEO_MARINE_URL", "").strip()
                           or DEFAULT_MARINE_URL)
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("OPEN_METEO_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("OPEN_METEO_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self._http = http_client

    # ------------------------------------------------------------------ fetches
    async def fetch_weather(self, latitude: float, longitude: float,
                            *, forecast_hours: int = 24) -> WeatherResponse:
        """Current conditions + hourly forecast for one coordinate."""
        params: Dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": _WEATHER_CURRENT_VARS,
            "hourly": _WEATHER_HOURLY_VARS,
            "forecast_days": max(1, min(2, (forecast_hours + 23) // 24)),
            "timezone": "UTC",
        }
        payload = await self._get_json(self.weather_url, params)
        try:
            return WeatherResponse.model_validate(payload)
        except ValidationError as exc:
            raise OpenMeteoInvalidResponse(f"weather response failed validation: {exc}") from exc

    async def fetch_marine(self, latitude: float, longitude: float) -> MarineResponse:
        """Current sea state (waves / swell / sea level) for one coordinate."""
        params: Dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "current": _MARINE_VARS,
            "hourly": _MARINE_VARS,
            "forecast_days": 1,
            "timezone": "UTC",
        }
        payload = await self._get_json(self.marine_url, params)
        try:
            return MarineResponse.model_validate(payload)
        except ValidationError as exc:
            raise OpenMeteoInvalidResponse(f"marine response failed validation: {exc}") from exc

    # ------------------------------------------------------------------ plumbing
    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx and on a 200 that is not a JSON object."""
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: OpenMeteoError = OpenMeteoUnavailable(f"no attempt made against {url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = OpenMeteoTimeout(
                        f"Open-Meteo timed out after {self.timeout_s}s: {url}")
                    log.warning("openmeteo_timeout", url=url, attempt=attempt, error=str(exc))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = OpenMeteoUnavailable(f"Open-Meteo unreachable: {exc}")
                    log.warning("openmeteo_unreachable", url=url, attempt=attempt, error=str(exc))
                    continue

                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise OpenMeteoInvalidResponse("Open-Meteo returned non-JSON body") from exc
                    if not isinstance(payload, dict):
                        raise OpenMeteoInvalidResponse(
                            f"Open-Meteo returned a {type(payload).__name__}, expected an object")
                    return payload

                reason = _error_reason(resp)
                if resp.status_code >= 500:
                    last_exc = OpenMeteoHTTPError(resp.status_code, reason)
                    log.warning("openmeteo_5xx", url=url, attempt=attempt,
                                status=resp.status_code, reason=reason)
                    continue
                # 4xx — the request itself is wrong; retrying cannot help.
                raise OpenMeteoHTTPError(resp.status_code, reason)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """Open-Meteo error bodies look like ``{"error": true, "reason": "..."}``."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body.get("reason")
    except ValueError:
        pass
    return None


__all__ = ["OpenMeteoClient", "DEFAULT_WEATHER_URL", "DEFAULT_MARINE_URL"]
