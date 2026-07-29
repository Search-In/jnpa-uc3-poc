"""Typed Bhuvan WMS failure vocabulary.

Every failure the client can produce is one of these, so the router can catch
``BhuvanError`` in ONE place and degrade to its configured-fallback answer —
callers never have to know about httpx internals. Mirrors
:mod:`integrations.openaq.exceptions` exactly.
"""
from __future__ import annotations

from typing import Optional


class BhuvanError(Exception):
    """Base class for every Bhuvan WMS client failure."""


class BhuvanTimeout(BhuvanError):
    """The request exceeded the configured timeout budget (after retries)."""


class BhuvanUnavailable(BhuvanError):
    """Network-level failure — DNS, refused connection, TLS, dropped socket."""


class BhuvanHTTPError(BhuvanError):
    """A non-200 HTTP response from the WMS server. ``reason`` carries the
    server's error text when one is present (OGC servers usually answer a
    ServiceExceptionReport XML body).
    """

    def __init__(self, status_code: int, reason: Optional[str] = None) -> None:
        self.status_code = status_code
        self.reason = reason
        super().__init__(f"Bhuvan WMS HTTP {status_code}: {reason or 'no reason given'}")


class BhuvanInvalidResponse(BhuvanError):
    """A 200 response whose body is not a parseable WMS capabilities document
    (or is an OGC ServiceExceptionReport instead of capabilities)."""


class BhuvanLayerNotFound(BhuvanError):
    """The requested layer name is not advertised by the WMS server's
    GetCapabilities document — a configuration problem, not an outage."""


__all__ = [
    "BhuvanError",
    "BhuvanTimeout",
    "BhuvanUnavailable",
    "BhuvanHTTPError",
    "BhuvanInvalidResponse",
    "BhuvanLayerNotFound",
]
