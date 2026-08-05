"""TomTom Traffic HTTP client — the ONLY layer that talks to the TomTom APIs.

Requires an account + API key (https://developer.tomtom.com/). Everything is
env-driven — NO hardcoded credential, NO vendor URL in business code (mirrors
integrations.openweather.client):

    TOMTOM_API_KEY        the key; empty -> provider disabled (BACKEND-ONLY,
                          never shipped to the frontend / VITE variables)
    TOMTOM_FLOW_URL       (default
                          https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json)
    TOMTOM_INCIDENTS_URL  (default
                          https://api.tomtom.com/traffic/services/5/incidentDetails)
    TOMTOM_ROUTING_URL    (default https://api.tomtom.com/routing/1/calculateRoute)
    TOMTOM_TIMEOUT_S      per-attempt budget       (default 5.0)
    TOMTOM_RETRIES        retries AFTER the first try (default 2)

Failure contract: every failure surfaces as a typed
:class:`~integrations.tomtom.exceptions.TomTomError` subclass. Timeouts,
network errors and 5xx are retried with exponential backoff; 4xx (invalid key
= 401, forbidden = 403, rate limit = 429) fail fast — retrying a rejected
request cannot help and would burn the rate budget. 200 bodies are validated
through the pydantic schemas before anything downstream sees them. The API key
never appears in logs or exception messages (redacted everywhere).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    TomTomError,
    TomTomHTTPError,
    TomTomInvalidResponse,
    TomTomNotConfigured,
    TomTomTimeout,
    TomTomUnavailable,
)
from .schemas import FlowSegmentResponse, IncidentsResponse

log = get_logger("integrations.tomtom.client")

DEFAULT_FLOW_URL = (
    "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json")
DEFAULT_INCIDENTS_URL = "https://api.tomtom.com/traffic/services/5/incidentDetails"
DEFAULT_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute"

# Incident Details v5 requires an explicit fields projection; this asks for
# exactly what schemas.Incident.normalize() consumes.
_INCIDENT_FIELDS = ("{incidents{type,geometry{type,coordinates},"
                    "properties{iconCategory,magnitudeOfDelay,"
                    "events{description,code,iconCategory},"
                    "from,to,roadNumbers,delay}}}")


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


class TomTomClient:
    """Async client for the TomTom Traffic Flow / Incidents (+ Routing) APIs.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :class:`integrations.openweather.OpenWeatherClient`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        flow_url: Optional[str] = None,
        incidents_url: Optional[str] = None,
        routing_url: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.api_key = (api_key if api_key is not None
                        else env.get("TOMTOM_API_KEY", "")).strip()
        self.flow_url = (flow_url or env.get("TOMTOM_FLOW_URL", "").strip()
                         or DEFAULT_FLOW_URL)
        self.incidents_url = (incidents_url
                              or env.get("TOMTOM_INCIDENTS_URL", "").strip()
                              or DEFAULT_INCIDENTS_URL)
        self.routing_url = (routing_url or env.get("TOMTOM_ROUTING_URL", "").strip()
                            or DEFAULT_ROUTING_URL)
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("TOMTOM_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("TOMTOM_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self._http = http_client

    @property
    def configured(self) -> bool:
        """True when an API key is present — the provider participates at all."""
        return bool(self.api_key)

    # ------------------------------------------------------------------ flow
    async def fetch_flow(self, latitude: float, longitude: float) -> FlowSegmentResponse:
        """Traffic Flow for the road segment nearest one coordinate (km/h)."""
        if not self.configured:
            raise TomTomNotConfigured("TOMTOM_API_KEY is not set")
        params: Dict[str, Any] = {
            "point": f"{latitude},{longitude}",
            "unit": "KMPH",
            "key": self.api_key,
        }
        payload = await self._get_json(self.flow_url, params)
        try:
            return FlowSegmentResponse.model_validate(payload)
        except ValidationError as exc:
            raise TomTomInvalidResponse(
                f"TomTom flow response failed validation: {exc}") from exc

    # ------------------------------------------------------------- incidents
    async def fetch_incidents(
        self,
        min_lon: float,
        min_lat: float,
        max_lon: float,
        max_lat: float,
    ) -> IncidentsResponse:
        """Traffic Incidents currently active inside one bounding box."""
        if not self.configured:
            raise TomTomNotConfigured("TOMTOM_API_KEY is not set")
        params: Dict[str, Any] = {
            "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "fields": _INCIDENT_FIELDS,
            "language": "en-GB",
            "timeValidityFilter": "present",
            "key": self.api_key,
        }
        payload = await self._get_json(self.incidents_url, params)
        try:
            return IncidentsResponse.model_validate(payload)
        except ValidationError as exc:
            raise TomTomInvalidResponse(
                f"TomTom incidents response failed validation: {exc}") from exc

    # -------------------------------------------------------------- routing
    async def fetch_route(
        self,
        from_lat: float,
        from_lon: float,
        to_lat: float,
        to_lon: float,
    ) -> Dict[str, Any]:
        """Optional Routing API support: one traffic-aware route summary
        (lengthInMeters / travelTimeInSeconds / trafficDelayInSeconds)."""
        if not self.configured:
            raise TomTomNotConfigured("TOMTOM_API_KEY is not set")
        url = (f"{self.routing_url.rstrip('/')}/"
               f"{from_lat},{from_lon}:{to_lat},{to_lon}/json")
        params: Dict[str, Any] = {
            "traffic": "true",
            "travelMode": "truck",
            "key": self.api_key,
        }
        payload = await self._get_json(url, params)
        routes = payload.get("routes")
        if not isinstance(routes, list) or not routes:
            raise TomTomInvalidResponse("TomTom routing response carries no routes")
        summary = routes[0].get("summary")
        if not isinstance(summary, dict):
            raise TomTomInvalidResponse("TomTom routing response carries no summary")
        return {
            "length_m": summary.get("lengthInMeters"),
            "travel_time_s": summary.get("travelTimeInSeconds"),
            "traffic_delay_s": summary.get("trafficDelayInSeconds"),
            "departure_time": summary.get("departureTime"),
            "arrival_time": summary.get("arrivalTime"),
        }

    # ------------------------------------------------------------- plumbing
    def _redact(self, text: str) -> str:
        """The API key must never surface in logs or exception messages."""
        return text.replace(self.api_key, "***") if self.api_key else text

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx (401 bad key, 403 forbidden, 429 rate limit) and on
        a 200 that is not a JSON object."""
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: TomTomError = TomTomUnavailable(f"no attempt made against {url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = TomTomTimeout(
                        f"TomTom timed out after {self.timeout_s}s")
                    log.warning("tomtom_timeout", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = TomTomUnavailable(
                        f"TomTom unreachable: {self._redact(str(exc))}")
                    log.warning("tomtom_unreachable", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue

                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise TomTomInvalidResponse(
                            "TomTom returned non-JSON body") from exc
                    if not isinstance(payload, dict):
                        raise TomTomInvalidResponse(
                            f"TomTom returned a {type(payload).__name__}, "
                            "expected an object")
                    return payload

                reason = _error_reason(resp)
                if resp.status_code >= 500:
                    last_exc = TomTomHTTPError(resp.status_code, reason)
                    log.warning("tomtom_5xx", attempt=attempt,
                                status=resp.status_code, reason=reason)
                    continue
                # 4xx — invalid key (401) / forbidden (403) / rate limited (429);
                # retrying cannot help and would burn the daily call budget.
                raise TomTomHTTPError(resp.status_code, reason)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """TomTom error bodies vary by service:
    ``{"error": {"description": "..."}}`` (traffic) or
    ``{"detailedError": {"message": "..."}}`` (routing) or
    ``{"errorText": "..."}`` (legacy)."""
    try:
        body = resp.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict) and err.get("description"):
        return err["description"]
    if isinstance(err, str):
        return err
    detailed = body.get("detailedError")
    if isinstance(detailed, dict) and detailed.get("message"):
        return detailed["message"]
    return body.get("errorText")


__all__ = ["TomTomClient", "DEFAULT_FLOW_URL", "DEFAULT_INCIDENTS_URL",
           "DEFAULT_ROUTING_URL"]
