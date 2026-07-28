"""TomTom Traffic integration — Flow + Incidents (+ Routing) APIs
(account + API key required).

Layering mirrors :mod:`integrations.openweather` exactly:
  client.py     — the ONLY layer that speaks HTTP to api.tomtom.com
                  (timeouts, bounded retries, typed errors, key never logged)
  schemas.py    — pydantic views over the raw responses + normalisation into
                  the flat ``traffic`` / ``incidents`` blocks the rest of the
                  backend consumes (the raw TomTom shape is never exposed)
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the CACHED / DATABASE / SYNTHETIC rungs

The API key (TOMTOM_API_KEY) is BACKEND-ONLY: it is read from the process
environment, sent only from the gateway to api.tomtom.com, and never exposed
to the frontend (no VITE_ variable, no browser call).

Consumed by :mod:`services.traffic.TrafficService` — a TomTom outage degrades
the /api/traffic/current surface through its fallback chain, never breaks it.
"""
from __future__ import annotations

from .client import TomTomClient
from .exceptions import (
    TomTomError,
    TomTomHTTPError,
    TomTomInvalidResponse,
    TomTomNotConfigured,
    TomTomTimeout,
    TomTomUnavailable,
)
from .schemas import (
    FlowSegmentResponse,
    IncidentsResponse,
    congestion_level,
    incident_severity,
    incident_type,
)

__all__ = [
    "TomTomClient",
    "TomTomError",
    "TomTomNotConfigured",
    "TomTomTimeout",
    "TomTomUnavailable",
    "TomTomHTTPError",
    "TomTomInvalidResponse",
    "FlowSegmentResponse",
    "IncidentsResponse",
    "congestion_level",
    "incident_type",
    "incident_severity",
]
