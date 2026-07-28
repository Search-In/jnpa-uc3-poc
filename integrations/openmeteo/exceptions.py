"""Typed Open-Meteo failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``OpenMeteoError`` in ONE place and drop to the CACHED / SYNTHETIC rung —
callers never have to know about httpx internals.
"""
from __future__ import annotations

from typing import Optional


class OpenMeteoError(Exception):
    """Base class for every Open-Meteo client failure."""


class OpenMeteoTimeout(OpenMeteoError):
    """The request exceeded the configured timeout budget (after retries)."""


class OpenMeteoUnavailable(OpenMeteoError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class OpenMeteoHTTPError(OpenMeteoError):
    """A non-200 HTTP response. ``reason`` carries Open-Meteo's error message
    (the API returns ``{"error": true, "reason": "..."}`` on bad requests)."""

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"Open-Meteo HTTP {status_code}: {reason or 'no reason given'}")


class OpenMeteoInvalidResponse(OpenMeteoError):
    """A 200 response whose body is not valid JSON / does not match the schema."""


__all__ = [
    "OpenMeteoError",
    "OpenMeteoTimeout",
    "OpenMeteoUnavailable",
    "OpenMeteoHTTPError",
    "OpenMeteoInvalidResponse",
]
