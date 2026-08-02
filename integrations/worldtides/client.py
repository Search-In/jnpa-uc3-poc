"""WorldTides HTTP client — the ONLY layer that talks to worldtides.info.

Requires an account + API key (https://www.worldtides.info/developer — credits
are pay-per-call, ~USD 10 covers far more than a PoC needs). Everything is
env-driven — NO hardcoded credential, NO vendor URL in business code (mirrors
integrations.openweather.client):

    WORLDTIDES_API_KEY      the key; empty -> provider disabled (BACKEND-ONLY,
                            never shipped to the frontend / VITE variables)
    WORLDTIDES_BASE_URL     (default https://www.worldtides.info/api/v3)
    WORLDTIDES_TIMEOUT_S    per-attempt budget         (default 5.0)
    WORLDTIDES_RETRIES      retries AFTER the first try (default 2)

One call fetches heights AND extremes for ``days`` days from today
(``datum=MSL`` so tide heights line up with Open-Meteo's
``sea_level_height_msl`` and the analytic floor).

Failure contract: every failure surfaces as a typed
:class:`~integrations.worldtides.exceptions.WorldTidesError` subclass.
Timeouts, network errors and 5xx are retried with exponential backoff; 4xx
(invalid key = 401/403, rate limit = 429) fail fast — retrying a rejected
request cannot help and would burn the credit budget. WorldTides also embeds
an API-level ``status`` in 200 bodies; a body-level error is surfaced as
:class:`WorldTidesHTTPError` too. The key rides in the query string, so it is
REDACTED from every log line and exception message (same hygiene as
integrations.ulip.client).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError

from jnpa_shared.logging import get_logger

from .exceptions import (
    WorldTidesError,
    WorldTidesHTTPError,
    WorldTidesInvalidResponse,
    WorldTidesNotConfigured,
    WorldTidesTimeout,
    WorldTidesUnavailable,
)
from .schemas import WorldTidesResponse

log = get_logger("integrations.worldtides.client")

DEFAULT_URL = "https://www.worldtides.info/api/v3"
# Two days of heights+extremes: enough that "next high/low" exists even when
# the request lands just before midnight, at 2 credits/call it is still cheap.
DEFAULT_DAYS = 2


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


class WorldTidesClient:
    """Async client for the WorldTides v3 heights/extremes API.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :class:`integrations.openweather.OpenWeatherClient`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        days: int = DEFAULT_DAYS,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.api_key = (api_key if api_key is not None
                        else env.get("WORLDTIDES_API_KEY", "")).strip()
        self.base_url = (base_url or env.get("WORLDTIDES_BASE_URL", "").strip()
                         or DEFAULT_URL).rstrip("/")
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("WORLDTIDES_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("WORLDTIDES_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self.days = max(1, days)
        self._http = http_client

    @property
    def configured(self) -> bool:
        """True when an API key is present — the provider participates at all."""
        return bool(self.api_key)

    # ------------------------------------------------------------ secret hygiene
    def _redact(self, text: str) -> str:
        """The key rides in the query string — never let it into logs/errors."""
        return text.replace(self.api_key, "***") if self.api_key else text

    # ------------------------------------------------------------------ fetch
    async def fetch_tides(self, latitude: float, longitude: float) -> WorldTidesResponse:
        """Height samples + tide extremes for one coordinate (datum=MSL)."""
        if not self.configured:
            raise WorldTidesNotConfigured("WORLDTIDES_API_KEY is not set")
        params: Dict[str, Any] = {
            "heights": "",
            "extremes": "",
            "lat": latitude,
            "lon": longitude,
            "days": self.days,
            "datum": "MSL",
            "key": self.api_key,
        }
        payload = await self._get_json(params)
        try:
            parsed = WorldTidesResponse.model_validate(payload)
        except ValidationError as exc:
            raise WorldTidesInvalidResponse(
                f"WorldTides response failed validation: {self._redact(str(exc))}") from exc
        if not parsed.ok:
            # API-level error embedded in a 200 body (bad key / no credit / …).
            raise WorldTidesHTTPError(int(parsed.status or 0),
                                      self._redact(parsed.error or "api-level error"))
        return parsed

    # ------------------------------------------------------------------ plumbing
    async def _get_json(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx (401/403 bad key, 429 rate limit) and on a 200 that
        is not a JSON object. Every surfaced message is key-redacted."""
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: WorldTidesError = WorldTidesUnavailable(
            f"no attempt made against {self.base_url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(self.base_url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = WorldTidesTimeout(
                        f"WorldTides timed out after {self.timeout_s}s")
                    log.warning("worldtides_timeout", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = WorldTidesUnavailable(
                        f"WorldTides unreachable: {self._redact(str(exc))}")
                    log.warning("worldtides_unreachable", attempt=attempt,
                                error=self._redact(str(exc)))
                    continue

                if resp.status_code == 200:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise WorldTidesInvalidResponse(
                            "WorldTides returned non-JSON body") from exc
                    if not isinstance(payload, dict):
                        raise WorldTidesInvalidResponse(
                            f"WorldTides returned a {type(payload).__name__}, "
                            "expected an object")
                    return payload

                reason = _error_reason(resp)
                if resp.status_code >= 500:
                    last_exc = WorldTidesHTTPError(
                        resp.status_code, self._redact(reason) if reason else None)
                    log.warning("worldtides_5xx", attempt=attempt,
                                status=resp.status_code,
                                reason=self._redact(reason or ""))
                    continue
                # 4xx — invalid key / bad request / rate limited; retrying cannot
                # help and would burn the per-call credit budget.
                raise WorldTidesHTTPError(
                    resp.status_code, self._redact(reason) if reason else None)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


def _error_reason(resp: httpx.Response) -> Optional[str]:
    """WorldTides error bodies look like ``{"status": 400, "error": "..."}``."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return body.get("error")
    except ValueError:
        pass
    return None


__all__ = ["WorldTidesClient", "DEFAULT_URL", "DEFAULT_DAYS"]
