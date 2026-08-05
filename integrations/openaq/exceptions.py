"""Typed OpenAQ Air Quality failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``OpenAQError`` in ONE place and drop to the CACHED / DATABASE /
SYNTHETIC rung — callers never have to know about httpx internals. Mirrors
:mod:`integrations.tomtom.exceptions` exactly.
"""
from __future__ import annotations

from typing import Optional


class OpenAQError(Exception):
    """Base class for every OpenAQ client failure."""


class OpenAQTimeout(OpenAQError):
    """The request exceeded the configured timeout budget (after retries)."""


class OpenAQUnavailable(OpenAQError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class OpenAQHTTPError(OpenAQError):
    """A non-200 HTTP response. ``reason`` carries OpenAQ's error message
    (the API returns ``{"detail": "..."}`` on rejected requests).

    Notable status codes (fail-fast, retrying cannot help):
      401/403 — the platform requires/rejects an API key (send OPENAQ_API_KEY)
      404     — unknown location id
      429     — rate limit exceeded
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"OpenAQ HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


class OpenAQInvalidResponse(OpenAQError):
    """A 200 response whose body is not valid JSON / does not match the schema."""


class OpenAQNoData(OpenAQError):
    """The platform answered but carries no station/measurement near the
    coordinate — an empty results set, not an outage. The service treats it
    like any other client failure and degrades to the next rung."""


__all__ = [
    "OpenAQError",
    "OpenAQTimeout",
    "OpenAQUnavailable",
    "OpenAQHTTPError",
    "OpenAQInvalidResponse",
    "OpenAQNoData",
]
