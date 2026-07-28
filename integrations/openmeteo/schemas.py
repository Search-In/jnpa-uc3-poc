"""Pydantic views over the raw Open-Meteo Weather / Marine responses.

Validation is deliberately tolerant (``extra="ignore"``, every variable
Optional): Open-Meteo adds fields over time and omits variables it cannot
model for a location — a partial answer must degrade a field to ``null``,
never fail the whole request. What IS enforced: the envelope carries echoed
coordinates and at least one of ``current`` / ``hourly`` (else the body is
not an Open-Meteo forecast at all and the client raises
:class:`~integrations.openmeteo.exceptions.OpenMeteoInvalidResponse`).

``normalize()`` flattens each response into the compact block the backend
consumes (``temperature`` / ``wind_speed`` / ``wave_height`` …, Open-Meteo
default units: °C, km/h, mm, m, s, visibility in metres). Variables that only
exist hourly (e.g. ``visibility``, marine ``sea_level_height_msl``) are read
from the hourly series at the current hour.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# WMO 4677 weather interpretation codes, as documented by Open-Meteo.
WMO_WEATHER_CODES: Dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def weather_condition(code: Optional[int]) -> Optional[str]:
    """Human-readable condition for a WMO weather code (None-safe)."""
    if code is None:
        return None
    return WMO_WEATHER_CODES.get(int(code), f"Unknown (WMO {code})")


def _hourly_at(times: List[str], values: List[Optional[float]],
               when: Optional[str]) -> Optional[float]:
    """The hourly value at the hour of ``when`` (ISO8601), else the first non-null.

    Open-Meteo hourly timestamps are hour-resolution ISO strings; matching on the
    ``YYYY-MM-DDTHH`` prefix pairs the current observation with its hourly slot.
    """
    if not values:
        return None
    if when and times:
        prefix = when[:13]
        for i, t in enumerate(times):
            if t[:13] == prefix and i < len(values) and values[i] is not None:
                return values[i]
    return next((v for v in values if v is not None), None)


# ------------------------------------------------------------------- weather
class CurrentWeather(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: Optional[str] = None
    temperature_2m: Optional[float] = None
    wind_speed_10m: Optional[float] = None
    wind_direction_10m: Optional[float] = None
    wind_gusts_10m: Optional[float] = None
    precipitation: Optional[float] = None
    weather_code: Optional[int] = None


class HourlyWeather(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: List[str] = []
    temperature_2m: List[Optional[float]] = []
    wind_speed_10m: List[Optional[float]] = []
    wind_direction_10m: List[Optional[float]] = []
    wind_gusts_10m: List[Optional[float]] = []
    precipitation: List[Optional[float]] = []
    weather_code: List[Optional[int]] = []
    visibility: List[Optional[float]] = []


class WeatherResponse(BaseModel):
    """One validated ``/v1/forecast`` envelope."""

    model_config = ConfigDict(extra="ignore")

    latitude: float
    longitude: float
    current: Optional[CurrentWeather] = None
    hourly: Optional[HourlyWeather] = None

    @model_validator(mode="after")
    def _require_some_data(self) -> "WeatherResponse":
        if self.current is None and self.hourly is None:
            raise ValueError("response carries neither 'current' nor 'hourly' data")
        return self

    def normalize(self) -> Dict[str, Any]:
        """Flatten into the backend's weather block (Open-Meteo default units)."""
        cur = self.current or CurrentWeather()
        hourly = self.hourly or HourlyWeather()
        code = cur.weather_code
        if code is None:
            raw = _hourly_at(hourly.time, list(hourly.weather_code), cur.time)
            code = int(raw) if raw is not None else None
        return {
            "temperature": cur.temperature_2m
            if cur.temperature_2m is not None
            else _hourly_at(hourly.time, hourly.temperature_2m, cur.time),
            "wind_speed": cur.wind_speed_10m
            if cur.wind_speed_10m is not None
            else _hourly_at(hourly.time, hourly.wind_speed_10m, cur.time),
            "wind_direction": cur.wind_direction_10m
            if cur.wind_direction_10m is not None
            else _hourly_at(hourly.time, hourly.wind_direction_10m, cur.time),
            "wind_gusts": cur.wind_gusts_10m
            if cur.wind_gusts_10m is not None
            else _hourly_at(hourly.time, hourly.wind_gusts_10m, cur.time),
            # Visibility is an hourly-only variable — always read from the series.
            "visibility": _hourly_at(hourly.time, hourly.visibility, cur.time),
            "precipitation": cur.precipitation
            if cur.precipitation is not None
            else _hourly_at(hourly.time, hourly.precipitation, cur.time),
            "weather_code": code,
            "condition": weather_condition(code),
            "observed_at": cur.time,
        }

    def forecast(self, hours: int) -> List[Dict[str, Any]]:
        """The next ``hours`` hourly entries starting at the current hour."""
        hourly = self.hourly
        if hours <= 0 or hourly is None or not hourly.time:
            return []
        start = 0
        when = self.current.time if self.current else None
        if when:
            prefix = when[:13]
            start = next((i for i, t in enumerate(hourly.time) if t[:13] == prefix), 0)

        def _pick(series: List[Any], i: int) -> Any:
            return series[i] if i < len(series) else None

        out: List[Dict[str, Any]] = []
        for i in range(start, min(start + hours, len(hourly.time))):
            code = _pick(hourly.weather_code, i)
            out.append({
                "time": hourly.time[i],
                "temperature": _pick(hourly.temperature_2m, i),
                "wind_speed": _pick(hourly.wind_speed_10m, i),
                "wind_direction": _pick(hourly.wind_direction_10m, i),
                "wind_gusts": _pick(hourly.wind_gusts_10m, i),
                "visibility": _pick(hourly.visibility, i),
                "precipitation": _pick(hourly.precipitation, i),
                "weather_code": code,
                "condition": weather_condition(code),
            })
        return out


# -------------------------------------------------------------------- marine
class CurrentMarine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: Optional[str] = None
    wave_height: Optional[float] = None
    wave_period: Optional[float] = None
    swell_wave_height: Optional[float] = None
    sea_level_height_msl: Optional[float] = None


class HourlyMarine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    time: List[str] = []
    wave_height: List[Optional[float]] = []
    wave_period: List[Optional[float]] = []
    swell_wave_height: List[Optional[float]] = []
    sea_level_height_msl: List[Optional[float]] = []


class MarineResponse(BaseModel):
    """One validated ``/v1/marine`` envelope."""

    model_config = ConfigDict(extra="ignore")

    latitude: float
    longitude: float
    current: Optional[CurrentMarine] = None
    hourly: Optional[HourlyMarine] = None

    @model_validator(mode="after")
    def _require_some_data(self) -> "MarineResponse":
        if self.current is None and self.hourly is None:
            raise ValueError("response carries neither 'current' nor 'hourly' data")
        return self

    def normalize(self) -> Dict[str, Any]:
        """Flatten into the backend's marine block (metres / seconds)."""
        cur = self.current or CurrentMarine()
        hourly = self.hourly or HourlyMarine()
        return {
            "wave_height": cur.wave_height
            if cur.wave_height is not None
            else _hourly_at(hourly.time, hourly.wave_height, cur.time),
            "wave_period": cur.wave_period
            if cur.wave_period is not None
            else _hourly_at(hourly.time, hourly.wave_period, cur.time),
            "swell_wave_height": cur.swell_wave_height
            if cur.swell_wave_height is not None
            else _hourly_at(hourly.time, hourly.swell_wave_height, cur.time),
            "sea_level_height": cur.sea_level_height_msl
            if cur.sea_level_height_msl is not None
            else _hourly_at(hourly.time, hourly.sea_level_height_msl, cur.time),
            "observed_at": cur.time,
        }


__all__ = [
    "WMO_WEATHER_CODES",
    "weather_condition",
    "CurrentWeather",
    "HourlyWeather",
    "WeatherResponse",
    "CurrentMarine",
    "HourlyMarine",
    "MarineResponse",
]
