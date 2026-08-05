"""Weather module — Open-Meteo Weather + Marine + OpenWeatherMap conditions
for the port area.

Layering mirrors services.customs / services.shipping_lines:
  repository — the only SQL speaker for core.weather_reading (raw SQL, no ORM)
  service    — fetch + combine + normalise + fallback orchestration
               (LIVE → CACHED → SYNTHETIC, source metadata always attached)

The external HTTP seams live in :mod:`integrations.openmeteo` and
:mod:`integrations.openweather` (client, schemas, typed exceptions).
OpenWeather is additive and key-gated (OPENWEATHER_API_KEY) — unconfigured,
the module behaves exactly as the original Open-Meteo-only build.
Schema: infra/postgres/v3/0105_weather_reading.sql + 0106_weather_openweather.sql
(dev bootstrap: gateway/weather_ext.ensure_weather_schema, JNPA_RUNTIME_DDL-gated).
"""
from __future__ import annotations

from .repository import WeatherRepository
from .service import WeatherService

__all__ = ["WeatherService", "WeatherRepository"]
