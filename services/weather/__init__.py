"""Weather module — Open-Meteo Weather + Marine conditions for the port area.

Layering mirrors services.customs / services.shipping_lines:
  repository — the only SQL speaker for core.weather_reading (raw SQL, no ORM)
  service    — fetch + combine + normalise + fallback orchestration
               (LIVE → CACHED → SYNTHETIC, source metadata always attached)

The external HTTP seam lives in :mod:`integrations.openmeteo` (client, schemas,
typed exceptions). Schema: infra/postgres/v3/0105_weather_reading.sql
(dev bootstrap: gateway/weather_ext.ensure_weather_schema, JNPA_RUNTIME_DDL-gated).
"""
from __future__ import annotations

from .repository import WeatherRepository
from .service import WeatherService

__all__ = ["WeatherService", "WeatherRepository"]
