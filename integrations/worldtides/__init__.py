"""WorldTides integration — tide heights + extremes API (account + key required).

Layering mirrors :mod:`integrations.openweather` exactly:
  client.py     — the ONLY layer that speaks HTTP to worldtides.info
                  (timeouts, bounded retries, typed errors, key REDACTED from
                  every log line and exception — it rides in the query string)
  schemas.py    — pydantic views over the raw response + normalisation into the
                  flat ``tide`` block the rest of the backend consumes
  exceptions.py — typed failure vocabulary so the service layer can map any
                  client failure onto the tide fallback ladder
                  (WorldTides LIVE → Open-Meteo marine sea level → CACHED →
                  DATABASE → ANALYTIC model)

The API key (WORLDTIDES_API_KEY) is BACKEND-ONLY: it is read from the process
environment, sent only from the gateway to worldtides.info, and never exposed
to the frontend (no VITE_ variable, no browser call).

Consumed by :mod:`services.weather.WeatherService` alongside Open-Meteo and
OpenWeatherMap — a WorldTides outage degrades only the ``tide`` block, never
the API.
"""
from __future__ import annotations

from .client import WorldTidesClient
from .exceptions import (
    WorldTidesError,
    WorldTidesHTTPError,
    WorldTidesInvalidResponse,
    WorldTidesNotConfigured,
    WorldTidesTimeout,
    WorldTidesUnavailable,
)
from .schemas import (
    MAX_SAMPLE_AGE_S,
    TIDE_FALLING,
    TIDE_RISING,
    WorldTidesExtreme,
    WorldTidesHeight,
    WorldTidesResponse,
)

__all__ = [
    "WorldTidesClient",
    "WorldTidesError",
    "WorldTidesNotConfigured",
    "WorldTidesTimeout",
    "WorldTidesUnavailable",
    "WorldTidesHTTPError",
    "WorldTidesInvalidResponse",
    "WorldTidesResponse",
    "WorldTidesHeight",
    "WorldTidesExtreme",
    "MAX_SAMPLE_AGE_S",
    "TIDE_RISING",
    "TIDE_FALLING",
]
