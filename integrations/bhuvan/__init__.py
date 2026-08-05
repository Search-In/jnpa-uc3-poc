"""Bhuvan WMS integration — ISRO/NRSC geospatial map layers over OGC WMS
(no API key, no account).

Layering mirrors :mod:`integrations.openaq` exactly:
  client.py     — the ONLY layer that speaks HTTP to the Bhuvan WMS server
                  (timeouts, bounded retries, typed errors)
  schemas.py    — pydantic views over the GetCapabilities XML + the parser
                  (the raw OGC XML is never exposed downstream)
  exceptions.py — typed failure vocabulary so the router can map any client
                  failure onto its configured-fallback answer

Control-plane only: the gateway NEVER downloads map imagery. It validates WMS
availability (GetCapabilities), fetches layer metadata, and hands the
configuration to the frontend — the browser renders Bhuvan GetMap tiles
directly on the ArcGIS map (see web/src/map/BhuvanWmsLayer.ts).

Consumed by :mod:`gateway.routers.bhuvan` (/api/bhuvan/health + /layers) —
a Bhuvan outage degrades those surfaces to their configured fallback, never
breaks them.
"""
from __future__ import annotations

from .client import BhuvanClient, DEFAULT_LAYER, DEFAULT_WMS_URL
from .exceptions import (
    BhuvanError,
    BhuvanHTTPError,
    BhuvanInvalidResponse,
    BhuvanLayerNotFound,
    BhuvanTimeout,
    BhuvanUnavailable,
)
from .schemas import WmsCapabilities, WmsLayer, parse_capabilities

__all__ = [
    "BhuvanClient",
    "DEFAULT_WMS_URL",
    "DEFAULT_LAYER",
    "BhuvanError",
    "BhuvanTimeout",
    "BhuvanUnavailable",
    "BhuvanHTTPError",
    "BhuvanInvalidResponse",
    "BhuvanLayerNotFound",
    "WmsCapabilities",
    "WmsLayer",
    "parse_capabilities",
]
