"""Typed JNPA Port-Data API failure vocabulary.

Every failure the client can produce is one of these, so the sync layer can
catch ``JnpaError`` in ONE place and degrade (skip the group, log the defect,
keep the watermark) — callers never have to know about httpx internals.
Mirrors :mod:`integrations.ulip.exceptions`.
"""
from __future__ import annotations

from typing import Optional


class JnpaError(Exception):
    """Base class for every JNPA Port-Data client failure."""


class JnpaNotConfigured(JnpaError):
    """No JNPA_PORTDATA_CLIENT_KEY configured — the provider is disabled,
    not broken."""


class JnpaTimeout(JnpaError):
    """The request exceeded the configured timeout budget (after retries)."""


class JnpaUnavailable(JnpaError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket.
    (The live endpoint is known to sit behind a port filter; this is the
    error that surfaces while it stays filtered.)"""


class JnpaAuthError(JnpaError):
    """Authentication failed — the client key was refused at /v2/auth/token,
    or a bearer token was rejected twice (once after a forced re-auth).
    Retrying with the same key cannot help. NOTE: the API deliberately slows
    the bad-key path (~250 ms) — never busy-loop on this error."""


class JnpaRateLimited(JnpaError):
    """HTTP 429 persisted through the retry budget. The API sends NO
    Retry-After / RateLimit-Reset header, so the retry already waited the
    blind 60 s + jitter window before surfacing this."""


class JnpaHTTPError(JnpaError):
    """A non-success HTTP response. ``error_code`` carries the API's ``error``
    body field (bad_parameter / bad_cursor / unknown_group / not_found /
    invalid_reference / internal_error ...) when the body exposes one."""

    def __init__(self, status_code: int, error_code: Optional[str] = None,
                 reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.error_code = error_code
        self.reason = reason
        super().__init__(
            f"JNPA API HTTP {status_code}"
            f" [{error_code or 'no-code'}]: {reason or 'no reason given'}")

    @property
    def is_bad_cursor(self) -> bool:
        """400 bad_cursor — the sync layer falls back to a fresh ``since``
        sweep (recordId dedup absorbs the re-read)."""
        return self.error_code == "bad_cursor"

    @property
    def is_unknown_group(self) -> bool:
        return self.error_code == "unknown_group"


class JnpaInvalidResponse(JnpaError):
    """A 2xx response whose body is not valid JSON / does not fit even the
    tolerant schemas."""


class JnpaChecksumMismatch(JnpaError):
    """Downloaded file bytes hash to a different sha256 than the record's
    ``checksumSha256`` / the response ``ETag`` — transfer corruption or a
    server-side defect. Always paired with a DefectObservation."""

    def __init__(self, file_ref: str, expected: Optional[str],
                 actual: str) -> None:
        self.file_ref = file_ref
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"checksum mismatch for {file_ref}: "
            f"expected {expected or '<none>'}, computed {actual}")


__all__ = [
    "JnpaError",
    "JnpaNotConfigured",
    "JnpaTimeout",
    "JnpaUnavailable",
    "JnpaAuthError",
    "JnpaRateLimited",
    "JnpaHTTPError",
    "JnpaInvalidResponse",
    "JnpaChecksumMismatch",
]
