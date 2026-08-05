"""Pydantic views over the raw OpenWeatherMap current-weather response.

Validation is deliberately tolerant (``extra="ignore"``, every variable
Optional) in the same spirit as :mod:`integrations.openmeteo.schemas`:
OpenWeatherMap omits blocks it has no data for (``rain`` only appears while it
is raining) — a partial answer must degrade a field to ``null`` / ``0``, never
fail the whole request. What IS enforced: the envelope carries at least one of
``main`` / ``weather`` (else the body is not a current-weather answer at all
and the client raises
:class:`~integrations.openweather.exceptions.OpenWeatherInvalidResponse`).

``normalize()`` flattens the response into the compact ``openweather`` block
the backend consumes. Units (with ``units=metric``): °C, %, mm (last 1 h),
hPa; OpenWeatherMap wind arrives in m/s and is converted to km/h so it is
directly comparable with the Open-Meteo weather block.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# OpenWeatherMap condition groups (first digit of the condition ``id``) mapped
# to the operational dashboard label. https://openweathermap.org/weather-conditions
_GROUP_LABELS: Dict[int, str] = {
    2: "STORM",           # 2xx Thunderstorm
    3: "RAIN",            # 3xx Drizzle
    5: "RAIN",            # 5xx Rain
    6: "SNOW",            # 6xx Snow
    7: "LOW_VISIBILITY",  # 7xx Atmosphere (mist / haze / fog / dust)
}

# Friendly display condition per OpenWeatherMap ``weather[0].main`` group.
_MAIN_CONDITIONS: Dict[str, str] = {
    "Clear": "Clear",
    "Clouds": "Cloudy",
    "Rain": "Rain",
    "Drizzle": "Drizzle",
    "Thunderstorm": "Thunderstorm",
    "Snow": "Snow",
    "Mist": "Mist",
    "Haze": "Haze",
    "Fog": "Fog",
    "Dust": "Dust",
    "Smoke": "Smoke",
    "Squall": "Squall",
    "Tornado": "Tornado",
}


def condition_label(condition_id: Optional[int]) -> Optional[str]:
    """Operational dashboard label (CLEAR / CLOUDY / RAIN / STORM / SNOW /
    LOW_VISIBILITY) for an OpenWeatherMap condition id (None-safe)."""
    if condition_id is None:
        return None
    if condition_id == 800:
        return "CLEAR"
    if 801 <= condition_id <= 804:
        return "CLOUDY"
    return _GROUP_LABELS.get(condition_id // 100, "UNKNOWN")


class WeatherCondition(BaseModel):
    """One entry of the ``weather`` array (id / group / description)."""

    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    main: Optional[str] = None
    description: Optional[str] = None


class MainBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    temp: Optional[float] = None          # °C (units=metric)
    feels_like: Optional[float] = None    # °C
    pressure: Optional[float] = None      # hPa
    humidity: Optional[float] = None      # %


class WindBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    speed: Optional[float] = None         # m/s (units=metric)
    deg: Optional[float] = None
    gust: Optional[float] = None          # m/s


class CloudsBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    all: Optional[float] = None           # % cloud cover


class RainBlock(BaseModel):
    """``rain`` only appears in the envelope while precipitation is falling."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    one_h: Optional[float] = None         # mm over the last hour

    @model_validator(mode="before")
    @classmethod
    def _lift_1h(cls, data: Any) -> Any:
        # The API key is literally "1h", which is not a valid identifier.
        if isinstance(data, dict) and "1h" in data:
            data = {**data, "one_h": data["1h"]}
        return data


class OpenWeatherResponse(BaseModel):
    """One validated ``/data/2.5/weather`` envelope."""

    model_config = ConfigDict(extra="ignore")

    weather: List[WeatherCondition] = []
    main: Optional[MainBlock] = None
    wind: Optional[WindBlock] = None
    clouds: Optional[CloudsBlock] = None
    rain: Optional[RainBlock] = None
    visibility: Optional[float] = None    # metres, capped at 10 km by the API
    dt: Optional[int] = None              # unix UTC of the observation
    name: Optional[str] = None            # station / place name

    @model_validator(mode="after")
    def _require_some_data(self) -> "OpenWeatherResponse":
        if self.main is None and not self.weather:
            raise ValueError("response carries neither 'main' nor 'weather' data")
        return self

    def normalize(self) -> Dict[str, Any]:
        """Flatten into the backend's ``openweather`` block (metric units;
        wind converted m/s -> km/h to match the Open-Meteo weather block)."""
        main = self.main or MainBlock()
        wind = self.wind or WindBlock()
        cond = self.weather[0] if self.weather else WeatherCondition()
        condition = (_MAIN_CONDITIONS.get(cond.main or "")
                     or (cond.description.capitalize() if cond.description else None))
        observed_at = (datetime.fromtimestamp(self.dt, tz=timezone.utc).isoformat()
                       if self.dt is not None else None)
        return {
            "temperature": main.temp,
            "feels_like": main.feels_like,
            "humidity": main.humidity,
            "pressure": main.pressure,
            # No rain block simply means it is not raining — report 0, not null.
            "rain": self.rain.one_h if self.rain and self.rain.one_h is not None else 0.0,
            "clouds": self.clouds.all if self.clouds else None,
            "condition": condition,
            "condition_id": cond.id,
            "description": cond.description,
            "label": condition_label(cond.id),
            "wind_speed": round(wind.speed * 3.6, 1) if wind.speed is not None else None,
            "wind_direction": wind.deg,
            "visibility": self.visibility,
            "station": self.name or None,
            "observed_at": observed_at,
        }


__all__ = [
    "OpenWeatherResponse",
    "WeatherCondition",
    "MainBlock",
    "WindBlock",
    "CloudsBlock",
    "RainBlock",
    "condition_label",
]
