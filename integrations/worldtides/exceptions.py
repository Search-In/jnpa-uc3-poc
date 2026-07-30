"""Typed WorldTides failure vocabulary.

Every failure the client can produce is one of these, so the service layer can
catch ``WorldTidesError`` in ONE place and drop to the next tide rung
(Open-Meteo marine sea level → cached → analytic) — callers never have to know
about httpx internals. Mirrors :mod:`integrations.openweather.exceptions`
exactly.
"""
from __future__ import annotations

from typing import Optional


class WorldTidesError(Exception):
    """Base class for every WorldTides client failure."""


class WorldTidesNotConfigured(WorldTidesError):
    """No WORLDTIDES_API_KEY configured — the provider is disabled, not broken."""


class WorldTidesTimeout(WorldTidesError):
    """The request exceeded the configured timeout budget (after retries)."""


class WorldTidesUnavailable(WorldTidesError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class WorldTidesHTTPError(WorldTidesError):
    """A non-200 answer — transport status or the body-level ``status`` field
    (WorldTides embeds its error code in a 200 body as ``{"status": 4xx,
    "error": "..."}``). ``reason`` carries the API's error message.

    Notable codes (fail-fast, retrying cannot help):
      400 — malformed request
      401/403 — invalid key / no credit
      429 — rate limited
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"WorldTides HTTP {status_code}: {reason or 'no reason given'}")

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


class WorldTidesInvalidResponse(WorldTidesError):
    """A 200 response whose body is not valid JSON / does not match the schema."""


__all__ = [
    "WorldTidesError",
    "WorldTidesNotConfigured",
    "WorldTidesTimeout",
    "WorldTidesUnavailable",
    "WorldTidesHTTPError",
    "WorldTidesInvalidResponse",
]
