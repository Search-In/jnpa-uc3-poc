"""OpenAQ v3 HTTP client — the ONLY layer that talks to api.openaq.org.

No account is required for the JNPA deployment default; an API key is
OPTIONAL (the openaq.org platform hands them out free and rate-limits keyless
traffic harder). Everything is env-driven — NO hardcoded credential, NO vendor
URL in business code (mirrors integrations.tomtom.client):

    OPENAQ_API_URL          (default https://api.openaq.org/v3)
    OPENAQ_API_KEY          OPTIONAL key sent as X-API-Key when present
                            (BACKEND-ONLY, never shipped to the frontend)
    OPENAQ_TIMEOUT_S        per-attempt budget            (default 5.0)
    OPENAQ_RETRIES          retries AFTER the first try   (default 2)
    OPENAQ_RADIUS_M         station search radius, metres (default 25000 —
                            the v3 maximum)
    OPENAQ_MAX_LOCATIONS    stations merged per fetch     (default 3)

Failure contract: every failure surfaces as a typed
:class:`~integrations.openaq.exceptions.OpenAQError` subclass. Timeouts,
network errors and 5xx are retried with exponential backoff; 4xx (401/403 key
problems, 429 rate limit) fail fast — retrying a rejected request cannot help.
200 bodies are validated through the pydantic schemas before anything
downstream sees them. The (optional) API key never appears in logs or
exception messages (redacted everywhere).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    OpenAQError,
    OpenAQHTTPError,
    OpenAQInvalidResponse,
    OpenAQNoData,
    OpenAQTimeout,
    OpenAQUnavailable,
)
from .schemas import LatestResponse, LocationsResponse, normalize_latest

log = get_logger("integrations.openaq.client")

DEFAULT_API_URL = "https://api.openaq.org/v3"
DEFAULT_RADIUS_M = 25_000       # v3 caps the coordinates radius at 25 km
DEFAULT_MAX_LOCATIONS = 3


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


class OpenAQClient:
    """Async client for the OpenAQ v3 locations + latest-measurements APIs.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :class:`integrations.tomtom.TomTomClient`.
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        radius_m: Optional[int] = None,
        max_locations: Optional[int] = None,
        backoff_s: float = 0.25,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.base_url = (base_url or env.get("OPENAQ_API_URL", "").strip()
                         or DEFAULT_API_URL).rstrip("/")
        self.api_key = (api_key if api_key is not None
                        else env.get("OPENAQ_API_KEY", "")).strip()
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("OPENAQ_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("OPENAQ_RETRIES"), 2)))
        self.radius_m = (radius_m if radius_m is not None
                         else _as_int(env.get("OPENAQ_RADIUS_M"), DEFAULT_RADIUS_M))
        self.max_locations = (max_locations if max_locations is not None
                              else max(1, _as_int(env.get("OPENAQ_MAX_LOCATIONS"),
                                                  DEFAULT_MAX_LOCATIONS)))
        self.backoff_s = backoff_s
        self._http = http_client

    @property
    def configured(self) -> bool:
        """Always True — OpenAQ needs no API key, so the provider always
        participates (unlike the key-gated TomTom/OpenWeather clients)."""
        return True

    # --------------------------------------------------------------- latest
    async def fetch_latest(self, latitude: float, longitude: float) -> Dict[str, Any]:
        """Newest pollutant readings near one coordinate, already normalised
        into the flat ``air_quality`` block (pm25/pm10/no2/so2/co/o3 +
        air_quality_status + observed_at).

        Two-step v3 flow: nearby stations first, then the newest value per
        sensor for up to ``max_locations`` stations (nearest first — later
        stations only fill pollutants the nearer ones don't report).
        Raises :class:`OpenAQNoData` when no station/measurement exists near
        the coordinate — an empty sky is not an outage, but the service must
        still degrade to its next rung.
        """
        params: Dict[str, Any] = {
            "coordinates": f"{latitude},{longitude}",
            "radius": self.radius_m,
            "limit": max(self.max_locations, 10),
        }
        payload = await self._get_json(f"{self.base_url}/locations", params)
        try:
            locations = LocationsResponse.model_validate(payload)
        except ValidationError as exc:
            raise OpenAQInvalidResponse(
                f"OpenAQ locations response failed validation: {exc}") from exc
        candidates = [loc for loc in locations.results
                      if loc.id is not None and loc.sensors][: self.max_locations]
        if not candidates:
            raise OpenAQNoData(
                f"no OpenAQ station within {self.radius_m} m of "
                f"{latitude},{longitude}")

        latest_by_location: Dict[int, LatestResponse] = {}
        for loc in candidates:
            body = await self._get_json(
                f"{self.base_url}/locations/{loc.id}/latest", {"limit": 100})
            try:
                latest_by_location[int(loc.id)] = LatestResponse.model_validate(body)
            except ValidationError as exc:
                raise OpenAQInvalidResponse(
                    f"OpenAQ latest response failed validation: {exc}") from exc

        normalized = normalize_latest(candidates, latest_by_location)
        if all(normalized.get(k) is None
               for k in ("pm25", "pm10", "no2", "so2", "co", "o3")):
            raise OpenAQNoData(
                "OpenAQ stations near the coordinate report no pollutant values")
        return normalized

    # ------------------------------------------------------------- plumbing
    def _redact(self, text: str) -> str:
        """The (optional) API key must never surface in logs or exceptions."""
        return text.replace(self.api_key, "***") if self.api_key else text

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx (401/403 key problems, 429 rate limit) and on a 200
        that is not a JSON object."""
        headers = {"X-API-Key": self.api_key} if self.api_key else {}
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: OpenAQError = OpenAQUnavailable(f"no attempt made against {url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(url, params=params, headers=headers)
                except httpx.TimeoutException as exc:
                    last_exc = OpenAQTimeout(
                        f"OpenAQ timed out after {self.timeout_s}s")
                    log.warning("openaq_timeout", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = OpenAQUnavailable(
                        f"OpenAQ unreachable: {self._redact(str(exc))}")
                    log.warning("openaq_unreachable", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue

                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise OpenAQInvalidResponse(
                            "OpenAQ returned non-JSON body") from exc
                    if not isinstance(payload, dict):
                        raise OpenAQInvalidResponse(
                            f"OpenAQ returned a {type(payload).__name__}, "
                            "expected an object")
                    return payload

                reason = _error_reason(resp)
                if resp.status_code >= 500:
                    last_exc = OpenAQHTTPError(resp.status_code, reason)
                    log.warning("openaq_5xx", attempt=attempt,
                                status=resp.status_code, reason=reason)
                    continue
                # 4xx — key problems (401/403) / rate limited (429); retrying
                # cannot help and would burn the rate budget.
                raise OpenAQHTTPError(resp.status_code, reason)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """OpenAQ error bodies are ``{"detail": "..."}`` (FastAPI-style; the
    detail may also be a validation list — stringified in that case)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    if isinstance(detail, str):
        return detail
    if detail is not None:
        return str(detail)
    return body.get("message")


__all__ = ["OpenAQClient", "DEFAULT_API_URL", "DEFAULT_RADIUS_M",
           "DEFAULT_MAX_LOCATIONS"]
