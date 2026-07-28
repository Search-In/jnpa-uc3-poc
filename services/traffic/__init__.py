"""Traffic module — TomTom live traffic intelligence for the JNPA corridor.

Layering mirrors :mod:`services.weather` exactly:
  service.py    — TrafficService: orchestrates the TomTom client + repository
                  behind the LIVE -> CACHED -> DATABASE -> SYNTHETIC chain
  repository.py — the ONLY layer that speaks SQL to core.traffic_reading

Consumed by gateway/routers/traffic.py (/api/traffic/current + /health);
the external seam is integrations/tomtom (key-gated via TOMTOM_API_KEY,
backend-only, never exposed to the browser).
"""
from __future__ import annotations

from .repository import TrafficRepository
from .service import TrafficService

__all__ = ["TrafficService", "TrafficRepository"]
