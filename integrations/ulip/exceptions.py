"""Typed ULIP (Unified Logistics Interface Platform) failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``UlipError`` in ONE place and drop to the CACHED / DATABASE / FALLBACK
rung — callers never have to know about httpx internals. Mirrors
:mod:`integrations.tomtom.exceptions` exactly.
"""
from __future__ import annotations

from typing import Optional


class UlipError(Exception):
    """Base class for every ULIP client failure."""


class UlipNotConfigured(UlipError):
    """No ULIP credential configured (neither ULIP_API_KEY nor
    ULIP_CLIENT_ID + ULIP_CLIENT_SECRET) — the provider is disabled, not
    broken."""


class UlipTimeout(UlipError):
    """The request exceeded the configured timeout budget (after retries)."""


class UlipUnavailable(UlipError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class UlipAuthError(UlipError):
    """Authentication with ULIP failed — the /user/login call was rejected or
    an issued token was refused (401/403 even after one forced re-login).
    Retrying with the same credentials cannot help."""


class UlipHTTPError(UlipError):
    """A non-200 HTTP response from a ULIP API endpoint. ``reason`` carries
    ULIP's error message when the body exposes one.

    Notable status codes (fail-fast, retrying cannot help):
      401/403 — rejected token / credentials (surfaced as UlipAuthError after
                one forced re-login attempt)
      429     — rate limit exceeded (subscription call budget)
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"ULIP HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


class UlipInvalidResponse(UlipError):
    """A 200 response whose body is not valid JSON / does not match the
    tolerant envelope schema, or an envelope that reports an API-level error."""


__all__ = [
    "UlipError",
    "UlipNotConfigured",
    "UlipTimeout",
    "UlipUnavailable",
    "UlipAuthError",
    "UlipHTTPError",
    "UlipInvalidResponse",
]
