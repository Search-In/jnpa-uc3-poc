"""ULIP integration — Unified Logistics Interface Platform (DPIIT) gateway
APIs for logistics intelligence (account credentials required).

Layering mirrors :mod:`integrations.tomtom` exactly:
  client.py     — the ONLY layer that speaks HTTP to the ULIP gateway
                  (login-token auth, timeouts, bounded retries, typed errors,
                  credentials never logged)
  schemas.py    — pydantic view over the common ULIP answer envelope +
                  normalisation into the flat ``logistics event`` dicts the
                  rest of the backend consumes (the raw ULIP shape is never
                  exposed past the module)
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the CACHED / DATABASE / FALLBACK rungs

The credentials (ULIP_CLIENT_ID / ULIP_CLIENT_SECRET, or a pre-issued
ULIP_API_KEY) are BACKEND-ONLY: read from the process environment, sent only
from the gateway to the ULIP platform, and never exposed to the frontend
(no VITE_ variable, no browser call).

Consumed by :mod:`services.logistics.LogisticsService` — a ULIP outage
degrades the /api/logistics/* surfaces through their fallback chain, never
breaks them.

NOTE: distinct from the two pre-existing ULIP touchpoints, which are left
untouched: gateway/routers/ulip.py (the trucking-app SECONDARY GPS relay
proxy) and services/fastag (the /api/fastag/* FASTag vertical with its own
FASTAG_ULIP_* configuration).
"""
from __future__ import annotations

from .client import UlipClient
from .exceptions import (
    UlipAuthError,
    UlipError,
    UlipHTTPError,
    UlipInvalidResponse,
    UlipNotConfigured,
    UlipTimeout,
    UlipUnavailable,
)
from .schemas import (
    EVENT_CONTAINER_MOVEMENT,
    EVENT_TOLL_CROSSING,
    REF_TYPE_CONTAINER,
    REF_TYPE_VEHICLE,
    UlipEnvelope,
    normalize_container_events,
    normalize_vehicle_events,
)

__all__ = [
    "UlipClient",
    "UlipError",
    "UlipNotConfigured",
    "UlipTimeout",
    "UlipUnavailable",
    "UlipAuthError",
    "UlipHTTPError",
    "UlipInvalidResponse",
    "UlipEnvelope",
    "normalize_vehicle_events",
    "normalize_container_events",
    "REF_TYPE_VEHICLE",
    "REF_TYPE_CONTAINER",
    "EVENT_TOLL_CROSSING",
    "EVENT_CONTAINER_MOVEMENT",
]
