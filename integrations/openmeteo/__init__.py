"""Open-Meteo integration — free Weather + Marine forecast APIs (no key, no account).

Layering mirrors services.fastag's client/mapper split:
  client.py     — the ONLY layer that speaks HTTP to api.open-meteo.com /
                  marine-api.open-meteo.com (timeouts, bounded retries, typed errors)
  schemas.py    — pydantic views over the raw responses + normalisation into the
                  flat weather/marine blocks the rest of the backend consumes
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the CACHED / SYNTHETIC fallback rungs

Consumed by :mod:`services.weather.WeatherService` (LIVE → CACHED → SYNTHETIC).
"""
from __future__ import annotations

from .client import OpenMeteoClient
from .exceptions import (
    OpenMeteoError,
    OpenMeteoHTTPError,
    OpenMeteoInvalidResponse,
    OpenMeteoTimeout,
    OpenMeteoUnavailable,
)
from .schemas import MarineResponse, WeatherResponse, weather_condition

__all__ = [
    "OpenMeteoClient",
    "OpenMeteoError",
    "OpenMeteoTimeout",
    "OpenMeteoUnavailable",
    "OpenMeteoHTTPError",
    "OpenMeteoInvalidResponse",
    "WeatherResponse",
    "MarineResponse",
    "weather_condition",
]
