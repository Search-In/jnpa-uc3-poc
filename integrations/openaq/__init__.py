"""OpenAQ Air Quality integration — v3 locations + latest-measurements APIs
(no API key required; an optional OPENAQ_API_KEY raises the rate limit).

Layering mirrors :mod:`integrations.tomtom` exactly:
  client.py     — the ONLY layer that speaks HTTP to api.openaq.org
                  (timeouts, bounded retries, typed errors, key never logged)
  schemas.py    — pydantic views over the raw responses + normalisation into
                  the flat ``air_quality`` block the rest of the backend
                  consumes (the raw OpenAQ shape is never exposed)
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the CACHED / DATABASE / SYNTHETIC rungs

Backend-only: the browser never talks to api.openaq.org (no VITE_ variable,
no frontend call) — the AirQualityTile only ever calls the gateway.

Consumed by :mod:`services.air_quality.AirQualityService` — an OpenAQ outage
degrades the /api/air-quality/current surface through its fallback chain,
never breaks it.
"""
from __future__ import annotations

from .client import OpenAQClient
from .exceptions import (
    OpenAQError,
    OpenAQHTTPError,
    OpenAQInvalidResponse,
    OpenAQNoData,
    OpenAQTimeout,
    OpenAQUnavailable,
)
from .schemas import (
    LatestResponse,
    LocationsResponse,
    POLLUTANTS,
    aq_status,
    normalize_latest,
    pollutant_status,
)

__all__ = [
    "OpenAQClient",
    "OpenAQError",
    "OpenAQTimeout",
    "OpenAQUnavailable",
    "OpenAQHTTPError",
    "OpenAQInvalidResponse",
    "OpenAQNoData",
    "LocationsResponse",
    "LatestResponse",
    "POLLUTANTS",
    "aq_status",
    "pollutant_status",
    "normalize_latest",
]
