"""Pydantic views over the raw TomTom Traffic API responses.

Validation is deliberately tolerant (``extra="ignore"``, every field Optional)
in the same spirit as :mod:`integrations.openweather.schemas`: TomTom omits
blocks it has no data for — a partial answer must degrade a field to ``null``,
never fail the whole request. What IS enforced: the Flow envelope carries a
``flowSegmentData`` object (else the body is not a flow answer at all and the
client raises :class:`~integrations.tomtom.exceptions.TomTomInvalidResponse`).

``normalize()`` flattens each response into the compact ``traffic`` /
``incidents`` blocks the backend consumes — the raw TomTom shape is NEVER
exposed past this module. Units: speeds km/h, travel times / delay seconds.

``congestion_level()`` derives the operational LOW / MEDIUM / HIGH / SEVERE
label from the speed ratio ``current_speed / free_flow_speed``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# Congestion label thresholds on the current/free-flow speed ratio. A road
# closure is always SEVERE regardless of the reported speeds.
CONGESTION_LOW = 0.80      # ratio >= 0.80 -> LOW (near free flow)
CONGESTION_MEDIUM = 0.60   # ratio >= 0.60 -> MEDIUM
CONGESTION_HIGH = 0.40     # ratio >= 0.40 -> HIGH; below -> SEVERE

# TomTom incident ``iconCategory`` -> operational incident type label.
# https://developer.tomtom.com/traffic-api/documentation/traffic-incidents/incident-details
_ICON_CATEGORY_TYPES: Dict[int, str] = {
    0: "UNKNOWN",
    1: "ACCIDENT",
    2: "FOG",
    3: "DANGEROUS_CONDITIONS",
    4: "RAIN",
    5: "ICE",
    6: "JAM",
    7: "LANE_CLOSED",
    8: "ROAD_CLOSED",
    9: "ROAD_WORKS",
    10: "WIND",
    11: "FLOODING",
    14: "BROKEN_DOWN_VEHICLE",
}

# TomTom incident ``magnitudeOfDelay`` -> operational severity label.
_MAGNITUDE_SEVERITY: Dict[int, str] = {
    0: "UNKNOWN",
    1: "MINOR",
    2: "MODERATE",
    3: "MAJOR",
    4: "CLOSURE",   # "undefined" in TomTom terms — used for road closures
}


def congestion_level(current_speed: Optional[float],
                     free_flow_speed: Optional[float],
                     *, road_closure: bool = False) -> str:
    """LOW / MEDIUM / HIGH / SEVERE from the current/free-flow speed ratio.

    A closed road is SEVERE regardless of speeds; missing/zero speeds degrade
    to UNKNOWN rather than guessing.
    """
    if road_closure:
        return "SEVERE"
    if current_speed is None or free_flow_speed is None or free_flow_speed <= 0:
        return "UNKNOWN"
    ratio = current_speed / free_flow_speed
    if ratio >= CONGESTION_LOW:
        return "LOW"
    if ratio >= CONGESTION_MEDIUM:
        return "MEDIUM"
    if ratio >= CONGESTION_HIGH:
        return "HIGH"
    return "SEVERE"


def incident_type(icon_category: Optional[int]) -> str:
    """Operational incident type label for a TomTom iconCategory (None-safe)."""
    if icon_category is None:
        return "UNKNOWN"
    return _ICON_CATEGORY_TYPES.get(icon_category, "UNKNOWN")


def incident_severity(magnitude: Optional[int]) -> str:
    """Operational severity label for a TomTom magnitudeOfDelay (None-safe)."""
    if magnitude is None:
        return "UNKNOWN"
    return _MAGNITUDE_SEVERITY.get(magnitude, "UNKNOWN")


# ------------------------------------------------------------------- Flow API
class FlowSegmentData(BaseModel):
    """The ``flowSegmentData`` block of a Traffic Flow (v4) answer."""

    model_config = ConfigDict(extra="ignore")

    frc: Optional[str] = None                    # functional road class
    currentSpeed: Optional[float] = None         # km/h
    freeFlowSpeed: Optional[float] = None        # km/h
    currentTravelTime: Optional[float] = None    # seconds
    freeFlowTravelTime: Optional[float] = None   # seconds
    confidence: Optional[float] = None           # 0..1
    roadClosure: Optional[bool] = None


class FlowSegmentResponse(BaseModel):
    """One validated ``/flowSegmentData/absolute/10/json`` envelope."""

    model_config = ConfigDict(extra="ignore")

    flowSegmentData: Optional[FlowSegmentData] = None

    @model_validator(mode="after")
    def _require_flow_data(self) -> "FlowSegmentResponse":
        if self.flowSegmentData is None:
            raise ValueError("response carries no 'flowSegmentData' block")
        return self

    def normalize(self) -> Dict[str, Any]:
        """Flatten into the backend's ``traffic`` block (km/h, seconds)."""
        f = self.flowSegmentData or FlowSegmentData()
        closed = bool(f.roadClosure)
        delay: Optional[float] = None
        if f.currentTravelTime is not None and f.freeFlowTravelTime is not None:
            delay = max(0.0, round(f.currentTravelTime - f.freeFlowTravelTime, 1))
        return {
            "current_speed": f.currentSpeed,
            "free_flow_speed": f.freeFlowSpeed,
            "current_travel_time": f.currentTravelTime,
            "free_flow_travel_time": f.freeFlowTravelTime,
            "congestion_level": congestion_level(
                f.currentSpeed, f.freeFlowSpeed, road_closure=closed),
            "delay_seconds": delay,
            "road_closure": closed,
            "confidence": f.confidence,
            "road_class": f.frc,
        }


# -------------------------------------------------------------- Incidents API
class IncidentEvent(BaseModel):
    """One entry of an incident's ``events`` array (coded description)."""

    model_config = ConfigDict(extra="ignore")

    description: Optional[str] = None
    code: Optional[int] = None
    iconCategory: Optional[int] = None


class IncidentProperties(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iconCategory: Optional[int] = None
    magnitudeOfDelay: Optional[int] = None
    events: List[IncidentEvent] = []
    from_: Optional[str] = None
    to: Optional[str] = None
    roadNumbers: List[str] = []
    delay: Optional[float] = None                # seconds

    @model_validator(mode="before")
    @classmethod
    def _lift_from(cls, data: Any) -> Any:
        # The API key is literally "from", a Python keyword.
        if isinstance(data, dict) and "from" in data:
            data = {**data, "from_": data["from"]}
        return data


class Incident(BaseModel):
    """One GeoJSON Feature of an Incident Details (v5) answer."""

    model_config = ConfigDict(extra="ignore")

    properties: Optional[IncidentProperties] = None

    def normalize(self) -> Dict[str, Any]:
        p = self.properties or IncidentProperties()
        description = next(
            (e.description for e in p.events if e.description), None)
        road = ", ".join(p.roadNumbers) if p.roadNumbers else None
        if p.from_ or p.to:
            span = " → ".join(x for x in (p.from_, p.to) if x)
            road = f"{road} ({span})" if road else span
        return {
            "type": incident_type(p.iconCategory),
            "description": description,
            "severity": incident_severity(p.magnitudeOfDelay),
            "road": road,
            "delay": p.delay,
        }


class IncidentsResponse(BaseModel):
    """One validated ``/incidentDetails`` envelope (tolerant: no incidents is
    a perfectly valid answer — quiet roads are not an error)."""

    model_config = ConfigDict(extra="ignore")

    incidents: List[Incident] = []

    def normalize(self) -> List[Dict[str, Any]]:
        return [i.normalize() for i in self.incidents]


__all__ = [
    "FlowSegmentData",
    "FlowSegmentResponse",
    "Incident",
    "IncidentEvent",
    "IncidentProperties",
    "IncidentsResponse",
    "congestion_level",
    "incident_type",
    "incident_severity",
    "CONGESTION_LOW",
    "CONGESTION_MEDIUM",
    "CONGESTION_HIGH",
]
