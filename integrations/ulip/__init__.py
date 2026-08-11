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

This is the single client for every ULIP API the account is granted — FASTAG,
LDB, VAHAN, SARATHI and GATISHAKTI — so the login token, retry budget,
redaction rules and audit shape are shared rather than reimplemented per
vertical. :mod:`services.fastag` and ``gateway/routers/ldb.py`` consume it too.

NOTE: distinct from gateway/routers/ulip.py, which is left untouched — that is
the trucking-app SECONDARY GPS relay proxy and has nothing to do with these
APIs despite the shared name.
"""
from __future__ import annotations

from .client import DEFAULT_API_PATHS, STAGING_API_URL, UlipClient
from .exceptions import (
    UlipAccessDenied,
    UlipAuthError,
    UlipError,
    UlipHTTPError,
    UlipInvalidRequest,
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
    normalize_dl,
    normalize_rc,
    normalize_road_network,
    normalize_tag_status,
    normalize_toll_plazas,
    normalize_vahan_xml,
    normalize_vehicle_events,
)

__all__ = [
    "UlipClient",
    "DEFAULT_API_PATHS",
    "STAGING_API_URL",
    "UlipError",
    "UlipNotConfigured",
    "UlipTimeout",
    "UlipUnavailable",
    "UlipAuthError",
    "UlipAccessDenied",
    "UlipHTTPError",
    "UlipInvalidRequest",
    "UlipInvalidResponse",
    "UlipEnvelope",
    "normalize_vehicle_events",
    "normalize_container_events",
    "normalize_rc",
    "normalize_vahan_xml",
    "normalize_dl",
    "normalize_tag_status",
    "normalize_toll_plazas",
    "normalize_road_network",
    "REF_TYPE_VEHICLE",
    "REF_TYPE_CONTAINER",
    "EVENT_TOLL_CROSSING",
    "EVENT_CONTAINER_MOVEMENT",
]
