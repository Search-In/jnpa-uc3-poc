"""Air Quality service module (OpenAQ) — same split as services.traffic:

  service.py     — AirQualityService: LIVE (OpenAQ) -> CACHED (Redis) ->
                   DATABASE (core.air_quality_readings) -> SYNTHETIC
  repository.py  — the ONLY layer that speaks SQL to core.air_quality_readings

Consumed by gateway/routers/air_quality.py (/api/air-quality/*).
"""
from __future__ import annotations

from .repository import AirQualityRepository
from .service import AirQualityService

__all__ = ["AirQualityService", "AirQualityRepository"]
