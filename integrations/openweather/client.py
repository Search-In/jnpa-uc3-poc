"""OpenWeatherMap HTTP client — the ONLY layer that talks to the OpenWeather API.

Requires an account + API key (https://openweathermap.org/api). Everything is
env-driven — NO hardcoded credential, NO vendor URL in business code (mirrors
integrations.openmeteo.client):

    OPENWEATHER_API_KEY      the key; empty -> provider disabled (BACKEND-ONLY,
                             never shipped to the frontend / VITE variables)
    OPENWEATHER_URL          (default https://api.openweathermap.org/data/2.5/weather)
    OPENWEATHER_TIMEOUT_S    per-attempt budget       (default 5.0)
    OPENWEATHER_RETRIES      retries AFTER the first try (default 2)

Failure contract: every failure surfaces as a typed
:class:`~integrations.openweather.exceptions.OpenWeatherError` subclass.
Timeouts, network errors and 5xx are retried with exponential backoff; 4xx
(invalid key = 401, unknown location = 404, rate limit = 429) fail fast —
retrying a rejected request cannot help and would burn the rate budget.
200 bodies are validated through the pydantic schema before anything
downstream sees them. The API key never appears in logs.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    OpenWeatherError,
    OpenWeatherHTTPError,
    OpenWeatherInvalidResponse,
    OpenWeatherNotConfigured,
    OpenWeatherTimeout,
    OpenWeatherUnavailable,
)
from .schemas import OpenWeatherResponse

log = get_logger("integrations.openweather.client")

DEFAULT_URL = "https://api.openweathermap.org/data/2.5/weather"


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


class OpenWeatherClient:
    """Async client for the OpenWeatherMap current-weather API.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :class:`integrations.openmeteo.OpenMeteoClient`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        url: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.api_key = (api_key if api_key is not None
                        else env.get("OPENWEATHER_API_KEY", "")).strip()
        self.url = (url or env.get("OPENWEATHER_URL", "").strip() or DEFAULT_URL)
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("OPENWEATHER_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("OPENWEATHER_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self._http = http_client

    @property
    def configured(self) -> bool:
        """True when an API key is present — the provider participates at all."""
        return bool(self.api_key)

    # ------------------------------------------------------------------ fetch
    async def fetch_current(self, latitude: float, longitude: float) -> OpenWeatherResponse:
        """Current conditions for one coordinate (units=metric)."""
        if not self.configured:
            raise OpenWeatherNotConfigured("OPENWEATHER_API_KEY is not set")
        params: Dict[str, Any] = {
            "lat": latitude,
            "lon": longitude,
            "appid": self.api_key,
            "units": "metric",
        }
        payload = await self._get_json(params)
        try:
            return OpenWeatherResponse.model_validate(payload)
        except ValidationError as exc:
            raise OpenWeatherInvalidResponse(
                f"OpenWeather response failed validation: {exc}") from exc

    # ------------------------------------------------------------------ plumbing
    async def _get_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx (401 bad key, 429 rate limit) and on a 200 that is
        not a JSON object."""
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: OpenWeatherError = OpenWeatherUnavailable(
            f"no attempt made against {self.url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(self.url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = OpenWeatherTimeout(
                        f"OpenWeatherMap timed out after {self.timeout_s}s")
                    log.warning("openweather_timeout", attempt=attempt, error=str(exc))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = OpenWeatherUnavailable(f"OpenWeatherMap unreachable: {exc}")
                    log.warning("openweather_unreachable", attempt=attempt, error=str(exc))
                    continue

                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise OpenWeatherInvalidResponse(
                            "OpenWeatherMap returned non-JSON body") from exc
                    if not isinstance(payload, dict):
                        raise OpenWeatherInvalidResponse(
                            f"OpenWeatherMap returned a {type(payload).__name__}, "
                            "expected an object")
                    return payload

                reason = _error_reason(resp)
                if resp.status_code >= 500:
                    last_exc = OpenWeatherHTTPError(resp.status_code, reason)
                    log.warning("openweather_5xx", attempt=attempt,
                                status=resp.status_code, reason=reason)
                    continue
                # 4xx — invalid key / bad request / rate limited; retrying cannot
                # help and would burn the free-tier call budget.
                raise OpenWeatherHTTPError(resp.status_code, reason)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """OpenWeatherMap error bodies look like ``{"cod": 401, "message": "..."}``."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body.get("message")
    except ValueError:
        pass
    return None


__all__ = ["OpenWeatherClient", "DEFAULT_URL"]
