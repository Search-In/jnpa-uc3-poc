"""Logistics module — ULIP logistics intelligence for the JNPA corridor.

Layering mirrors :mod:`services.traffic` exactly:
  service.py    — LogisticsService: orchestrates the ULIP client + repository
                  behind the LIVE -> CACHED -> DATABASE -> FALLBACK chain
  repository.py — the ONLY layer that speaks SQL to core.logistics_event /
                  core.logistics_tracking / core.ulip_api_audit

Consumed by gateway/routers/logistics.py (/api/logistics/*); the external
seam is integrations/ulip (credential-gated via ULIP_CLIENT_ID +
ULIP_CLIENT_SECRET or a pre-issued ULIP_API_KEY, backend-only, never exposed
to the browser).
"""
from __future__ import annotations

from .repository import LogisticsRepository
from .service import LogisticsService

__all__ = ["LogisticsService", "LogisticsRepository"]
