"""Typed TomTom Traffic failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``TomTomError`` in ONE place and drop to the CACHED / DATABASE /
SYNTHETIC rung — callers never have to know about httpx internals. Mirrors
:mod:`integrations.openweather.exceptions` exactly.
"""
from __future__ import annotations

from typing import Optional


class TomTomError(Exception):
    """Base class for every TomTom Traffic client failure."""


class TomTomNotConfigured(TomTomError):
    """No TOMTOM_API_KEY configured — the provider is disabled, not broken."""


class TomTomTimeout(TomTomError):
    """The request exceeded the configured timeout budget (after retries)."""


class TomTomUnavailable(TomTomError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class TomTomHTTPError(TomTomError):
    """A non-200 HTTP response. ``reason`` carries TomTom's error message
    (the API returns ``{"error": {"description": "..."}}`` /
    ``{"detailedError": {"message": "..."}}`` on rejected requests).

    Notable status codes (fail-fast, retrying cannot help):
      401 — invalid / inactive API key
      403 — forbidden (key lacks the Traffic API entitlement)
      429 — rate limit exceeded (free tier: 2 500 requests/day)
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"TomTom HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


class TomTomInvalidResponse(TomTomError):
    """A 200 response whose body is not valid JSON / does not match the schema."""


__all__ = [
    "TomTomError",
    "TomTomNotConfigured",
    "TomTomTimeout",
    "TomTomUnavailable",
    "TomTomHTTPError",
    "TomTomInvalidResponse",
]
