"""OpenWeatherMap integration — current-weather API (account + API key required).

Layering mirrors :mod:`integrations.openmeteo` exactly:
  client.py     — the ONLY layer that speaks HTTP to api.openweathermap.org
                  (timeouts, bounded retries, typed errors, key never logged)
  schemas.py    — pydantic views over the raw response + normalisation into the
                  flat ``openweather`` block the rest of the backend consumes
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the CACHED / SYNTHETIC fallback rungs

The API key (OPENWEATHER_API_KEY) is BACKEND-ONLY: it is read from the process
environment, sent only from the gateway to api.openweathermap.org, and never
exposed to the frontend (no VITE_ variable, no browser call).

Consumed by :mod:`services.weather.WeatherService` alongside Open-Meteo — an
OpenWeather outage degrades only the ``openweather`` block, never the API.
"""
from __future__ import annotations

from .client import OpenWeatherClient
from .exceptions import (
    OpenWeatherError,
    OpenWeatherHTTPError,
    OpenWeatherInvalidResponse,
    OpenWeatherNotConfigured,
    OpenWeatherTimeout,
    OpenWeatherUnavailable,
)
from .schemas import OpenWeatherResponse, condition_label

__all__ = [
    "OpenWeatherClient",
    "OpenWeatherError",
    "OpenWeatherNotConfigured",
    "OpenWeatherTimeout",
    "OpenWeatherUnavailable",
    "OpenWeatherHTTPError",
    "OpenWeatherInvalidResponse",
    "OpenWeatherResponse",
    "condition_label",
]
