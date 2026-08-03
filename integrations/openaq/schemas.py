"""Pydantic views over the raw OpenAQ v3 API responses.

Validation is deliberately tolerant (``extra="ignore"``, every field Optional)
in the same spirit as :mod:`integrations.tomtom.schemas`: a monitoring station
that reports only some pollutants must degrade the missing fields to ``null``,
never fail the whole request.

The v3 flow is two-step: ``/v3/locations?coordinates=…`` lists the stations
near a point (each with its ``sensors`` — one per pollutant), then
``/v3/locations/{id}/latest`` returns the newest value per sensor id. The
client stitches the two together and hands this module the pieces;
``normalize_latest()`` flattens everything into the compact ``air_quality``
block the backend consumes — the raw OpenAQ shape is NEVER exposed past this
module. Units: OpenAQ reports Indian stations in µg/m³.

``aq_status()`` derives the operational GOOD / MODERATE / UNHEALTHY /
VERY_UNHEALTHY label from CPCB-inspired concentration breakpoints (worst
pollutant wins; nothing measured -> UNKNOWN).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

# Pollutants the JNPA surface consumes, keyed by OpenAQ parameter name.
POLLUTANTS = ("pm25", "pm10", "no2", "so2", "co", "o3")

STATUS_GOOD = "GOOD"
STATUS_MODERATE = "MODERATE"
STATUS_UNHEALTHY = "UNHEALTHY"
STATUS_VERY_UNHEALTHY = "VERY_UNHEALTHY"
STATUS_UNKNOWN = "UNKNOWN"

# CPCB-inspired breakpoints, µg/m³ (CO listed in µg/m³ too — OpenAQ reports
# Indian CO in µg/m³ or mg/m³; mg/m³ values are converted before classifying).
# Each tuple: (GOOD upper bound, MODERATE upper bound, UNHEALTHY upper bound);
# above the last bound -> VERY_UNHEALTHY.
_BREAKPOINTS: Dict[str, Tuple[float, float, float]] = {
    "pm25": (30.0, 60.0, 120.0),
    "pm10": (50.0, 100.0, 250.0),
    "no2": (40.0, 80.0, 180.0),
    "so2": (40.0, 80.0, 380.0),
    "o3": (50.0, 100.0, 168.0),
    "co": (1000.0, 2000.0, 10000.0),
}
_STATUS_RANK = {STATUS_GOOD: 0, STATUS_MODERATE: 1,
                STATUS_UNHEALTHY: 2, STATUS_VERY_UNHEALTHY: 3}


def pollutant_status(parameter: str, value: Optional[float]) -> str:
    """GOOD / MODERATE / UNHEALTHY / VERY_UNHEALTHY for ONE pollutant value
    (UNKNOWN when the value or the breakpoint table is missing)."""
    bounds = _BREAKPOINTS.get(parameter)
    if value is None or bounds is None:
        return STATUS_UNKNOWN
    good, moderate, unhealthy = bounds
    if value <= good:
        return STATUS_GOOD
    if value <= moderate:
        return STATUS_MODERATE
    if value <= unhealthy:
        return STATUS_UNHEALTHY
    return STATUS_VERY_UNHEALTHY


def aq_status(values: Dict[str, Optional[float]]) -> str:
    """Overall label: the WORST per-pollutant status across everything
    measured; UNKNOWN when nothing is measured (never guessed)."""
    worst: Optional[str] = None
    for parameter, value in values.items():
        label = pollutant_status(parameter, value)
        if label == STATUS_UNKNOWN:
            continue
        if worst is None or _STATUS_RANK[label] > _STATUS_RANK[worst]:
            worst = label
    return worst or STATUS_UNKNOWN


# ------------------------------------------------------------- /v3/locations
class SensorParameter(BaseModel):
    """The pollutant a sensor measures (``parameter`` block of a sensor)."""

    model_config = ConfigDict(extra="ignore")

    name: Optional[str] = None      # "pm25", "pm10", "no2", …
    units: Optional[str] = None     # "µg/m³", "ppm", …


class Sensor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    parameter: Optional[SensorParameter] = None


class Location(BaseModel):
    """One monitoring station of a ``/v3/locations`` answer."""

    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = None
    name: Optional[str] = None
    sensors: List[Sensor] = []


class LocationsResponse(BaseModel):
    """One validated ``/v3/locations?coordinates=…`` envelope (tolerant: an
    empty results list is a valid answer — the client raises OpenAQNoData,
    not a schema error)."""

    model_config = ConfigDict(extra="ignore")

    results: List[Location] = []


# --------------------------------------------------- /v3/locations/{id}/latest
class LatestDatetime(BaseModel):
    model_config = ConfigDict(extra="ignore")

    utc: Optional[str] = None


class LatestMeasurement(BaseModel):
    """One newest-value-per-sensor row of a ``…/latest`` answer."""

    model_config = ConfigDict(extra="ignore")

    sensorsId: Optional[int] = None
    value: Optional[float] = None
    datetime: Optional[LatestDatetime] = None


class LatestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    results: List[LatestMeasurement] = []


def normalize_latest(
    locations: List[Location],
    latest_by_location: Dict[int, LatestResponse],
) -> Dict[str, Any]:
    """Stitch stations + newest sensor values into the flat ``air_quality``
    block. Stations are visited in the order the API returned them (nearest
    first); the first station reporting a pollutant wins, later stations only
    fill pollutants still missing. mg/m³ CO values are converted to µg/m³ so
    the breakpoints apply uniformly."""
    values: Dict[str, Optional[float]] = {p: None for p in POLLUTANTS}
    observed_at: Optional[str] = None
    stations: List[str] = []

    for loc in locations:
        if loc.id is None:
            continue
        latest = latest_by_location.get(loc.id)
        if latest is None:
            continue
        by_sensor = {m.sensorsId: m for m in latest.results if m.sensorsId is not None}
        used = False
        for sensor in loc.sensors:
            param = sensor.parameter.name if sensor.parameter else None
            if param not in POLLUTANTS or sensor.id not in by_sensor:
                continue
            if values[param] is not None:
                continue
            m = by_sensor[sensor.id]
            if m.value is None:
                continue
            value = float(m.value)
            units = (sensor.parameter.units or "") if sensor.parameter else ""
            if param == "co" and "mg" in units.lower():
                value *= 1000.0
            values[param] = round(value, 2)
            used = True
            ts = m.datetime.utc if m.datetime else None
            if ts and (observed_at is None or ts > observed_at):
                observed_at = ts
        if used and loc.name:
            stations.append(loc.name)

    return {
        **values,
        "air_quality_status": aq_status(values),
        "source": "OPENAQ",
        "observed_at": observed_at,
        "stations": stations,
    }


__all__ = [
    "POLLUTANTS",
    "SensorParameter",
    "Sensor",
    "Location",
    "LocationsResponse",
    "LatestDatetime",
    "LatestMeasurement",
    "LatestResponse",
    "normalize_latest",
    "aq_status",
    "pollutant_status",
    "STATUS_GOOD",
    "STATUS_MODERATE",
    "STATUS_UNHEALTHY",
    "STATUS_VERY_UNHEALTHY",
    "STATUS_UNKNOWN",
]
