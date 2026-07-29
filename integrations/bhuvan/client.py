"""Bhuvan WMS HTTP client — the ONLY layer that talks to the ISRO/NRSC server.

Bhuvan is an OGC WMS map service: NO API key, NO account. The gateway never
downloads map imagery — the browser renders GetMap tiles directly on the
ArcGIS map. This client only performs the two lightweight control-plane
duties the backend owns:

  * availability probing        (GetCapabilities round-trip)
  * layer-metadata fetch        (named layers parsed out of the capabilities)

Everything is env-driven — NO hardcoded credential, and the vendor URL lives
only in the defaults below (mirrors integrations.openaq.client):

    BHUVAN_WMS_URL      WMS endpoint      (default bhuvan-vec1.nrsc.gov.in)
    BHUVAN_LAYER        default layer     (default india3 — the base mosaic)
    BHUVAN_TIMEOUT_S    per-attempt budget            (default 5.0)
    BHUVAN_RETRIES      retries AFTER the first try   (default 2)

Failure contract: every failure surfaces as a typed
:class:`~integrations.bhuvan.exceptions.BhuvanError` subclass. Timeouts,
network errors and 5xx are retried with exponential backoff; 4xx fail fast —
retrying a rejected request cannot help. 200 bodies are validated through
:func:`~integrations.bhuvan.schemas.parse_capabilities` before anything
downstream sees them.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx

from jnpa_shared.logging import get_logger

from .exceptions import (
    BhuvanError,
    BhuvanHTTPError,
    BhuvanLayerNotFound,
    BhuvanTimeout,
    BhuvanUnavailable,
)
from .schemas import WmsCapabilities, WmsLayer, parse_capabilities

log = get_logger("integrations.bhuvan.client")

DEFAULT_WMS_URL = "https://bhuvan-vec1.nrsc.gov.in/bhuvan/wms"
DEFAULT_LAYER = "india3"
# GetCapabilities documents can be large but are text-only; a hard cap keeps a
# misbehaving server from streaming an unbounded body into the gateway.
MAX_CAPABILITIES_BYTES = 8 * 1024 * 1024


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


class BhuvanClient:
    """Async client for the Bhuvan WMS GetCapabilities control plane.

    Stateless apart from configuration. An externally-owned
    ``httpx.AsyncClient`` may be injected (tests / the gateway's pooled
    client); otherwise a short-lived client is created per call, exactly like
    :class:`integrations.openaq.OpenAQClient`.
    """

    def __init__(
        self,
        *,
        wms_url: Optional[str] = None,
        default_layer: Optional[str] = None,
        timeout_s: Optional[float] = None,
        retries: Optional[int] = None,
        backoff_s: float = 0.25,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        env = os.environ
        self.wms_url = (wms_url or env.get("BHUVAN_WMS_URL", "").strip()
                        or DEFAULT_WMS_URL).rstrip("?& ")
        self.default_layer = (default_layer or env.get("BHUVAN_LAYER", "").strip()
                              or DEFAULT_LAYER)
        self.timeout_s = (timeout_s if timeout_s is not None
                          else _as_float(env.get("BHUVAN_TIMEOUT_S"), 5.0))
        self.retries = (retries if retries is not None
                        else max(0, _as_int(env.get("BHUVAN_RETRIES"), 2)))
        self.backoff_s = backoff_s
        self._http = http_client

    @property
    def configured(self) -> bool:
        """True when a WMS endpoint is known. Bhuvan needs no API key, so a
        non-empty URL (the built-in default suffices) is full configuration."""
        return bool(self.wms_url)

    # -------------------------------------------------------------- metadata
    async def fetch_capabilities(self) -> WmsCapabilities:
        """GetCapabilities → parsed :class:`WmsCapabilities` (named layers only)."""
        params = {"service": "WMS", "request": "GetCapabilities"}
        body = await self._get_text(self.wms_url, params)
        return parse_capabilities(body)

    async def validate_layer(self, name: Optional[str] = None) -> WmsLayer:
        """Confirm ``name`` (default: the configured layer) is advertised by
        the server. Raises :class:`BhuvanLayerNotFound` when it is not."""
        wanted = (name or self.default_layer).strip()
        caps = await self.fetch_capabilities()
        layer = caps.find_layer(wanted)
        if layer is None:
            raise BhuvanLayerNotFound(
                f"layer '{wanted}' is not advertised by {self.wms_url} "
                f"({len(caps.layers)} named layers found)")
        return layer

    async def check_availability(self) -> WmsCapabilities:
        """Health probe: one GetCapabilities round-trip. Any BhuvanError means
        the service is not currently usable; a parsed document means it is."""
        return await self.fetch_capabilities()

    # ------------------------------------------------------------- plumbing
    async def _get_text(self, url: str, params: dict) -> str:
        """GET with bounded retries. Retries timeouts / network errors / 5xx;
        fails fast on 4xx. Returns the (size-capped) response text."""
        client = self._http or httpx.AsyncClient(timeout=self.timeout_s)
        owns = self._http is None
        last_exc: BhuvanError = BhuvanUnavailable(f"no attempt made against {url}")
        try:
            for attempt in range(self.retries + 1):
                if attempt:
                    await asyncio.sleep(self.backoff_s * (2 ** (attempt - 1)))
                try:
                    resp = await client.get(url, params=params)
                except httpx.TimeoutException as exc:
                    last_exc = BhuvanTimeout(
                        f"Bhuvan WMS timed out after {self.timeout_s}s")
                    log.warning("bhuvan_timeout", attempt=attempt, error=str(exc))
                    continue
                except httpx.HTTPError as exc:
                    last_exc = BhuvanUnavailable(f"Bhuvan WMS unreachable: {exc}")
                    log.warning("bhuvan_unreachable", attempt=attempt, error=str(exc))
                    continue

                if resp.status_code == 200:
                    if len(resp.content) > MAX_CAPABILITIES_BYTES:
                        raise BhuvanHTTPError(
                            200, "capabilities document exceeds the size cap")
                    return resp.text

                if resp.status_code >= 500:
                    last_exc = BhuvanHTTPError(resp.status_code, "server error")
                    log.warning("bhuvan_5xx", attempt=attempt,
                                status=resp.status_code)
                    continue
                # 4xx — a bad request / blocked path; retrying cannot help.
                raise BhuvanHTTPError(resp.status_code,
                                      (resp.text or "")[:200] or None)
            raise last_exc
        finally:
            if owns:
                await client.aclose()


__all__ = ["BhuvanClient", "DEFAULT_WMS_URL", "DEFAULT_LAYER"]
