"""Typed OpenWeatherMap failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``OpenWeatherError`` in ONE place and drop to the CACHED / SYNTHETIC rung —
callers never have to know about httpx internals. Mirrors
:mod:`integrations.openmeteo.exceptions` exactly.
"""
from __future__ import annotations

from typing import Optional


class OpenWeatherError(Exception):
    """Base class for every OpenWeatherMap client failure."""


class OpenWeatherNotConfigured(OpenWeatherError):
    """No OPENWEATHER_API_KEY configured — the provider is disabled, not broken."""


class OpenWeatherTimeout(OpenWeatherError):
    """The request exceeded the configured timeout budget (after retries)."""


class OpenWeatherUnavailable(OpenWeatherError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class OpenWeatherHTTPError(OpenWeatherError):
    """A non-200 HTTP response. ``reason`` carries OpenWeatherMap's error message
    (the API returns ``{"cod": 401, "message": "..."}`` on rejected requests).

    Notable status codes (fail-fast, retrying cannot help):
      401 — invalid / inactive API key
      404 — unknown location
      429 — rate limit exceeded (free tier: 60 calls/min)
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"OpenWeatherMap HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code == 401

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


class OpenWeatherInvalidResponse(OpenWeatherError):
    """A 200 response whose body is not valid JSON / does not match the schema."""


__all__ = [
    "OpenWeatherError",
    "OpenWeatherNotConfigured",
    "OpenWeatherTimeout",
    "OpenWeatherUnavailable",
    "OpenWeatherHTTPError",
    "OpenWeatherInvalidResponse",
]
