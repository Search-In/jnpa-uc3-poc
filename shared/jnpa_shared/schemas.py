"""Pydantic v2 event/record models for the JNPA UC-III PoC.

These models are the wire contract for Kafka/MQTT messages and the row shape
for the corresponding Postgres tables. All timestamps are timezone-aware UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class _Base(BaseModel):
    model_config = ConfigDict(
        extra="ignore",
        ser_json_timedelta="iso8601",
        populate_by_name=True,
    )


class VehicleClass(str, Enum):
    HGV = "HGV"          # heavy goods vehicle / container truck
    LGV = "LGV"          # light goods vehicle
    CAR = "CAR"
    BUS = "BUS"
    TWO_WHEELER = "2W"
    UNKNOWN = "UNKNOWN"


class AnprRead(_Base):
    """Automatic Number Plate Recognition read from a camera."""

    ts: datetime = Field(default_factory=_utcnow)
    camera_id: str
    plate: str
    conf: float = Field(ge=0.0, le=1.0, description="recognition confidence 0..1")
    vehicle_class: VehicleClass = VehicleClass.UNKNOWN
    image_url: Optional[str] = None
    weather: Optional[str] = None
    degraded: bool = False


class VahanRecord(_Base):
    """Vehicle registration record sourced from Vahan / Surepass."""

    plate: str
    rc_type: Optional[str] = None
    owner_hash: Optional[str] = None
    fitness_valid_to: Optional[date] = None
    puc_valid_to: Optional[date] = None
    fastag_status: Optional[str] = None
    provisional: bool = False
    provisional_until: Optional[datetime] = None


class FastagPing(_Base):
    """A FASTag toll/gantry read for a vehicle."""

    ts: datetime = Field(default_factory=_utcnow)
    tag_id: str
    plate: Optional[str] = None
    reader_id: str
    plaza: Optional[str] = None
    balance: Optional[float] = None
    status: Optional[str] = None


class RfidRead(_Base):
    """RFID tag read from a corridor/gate reader."""

    ts: datetime = Field(default_factory=_utcnow)
    reader_id: str
    tag_id: str
    rssi: float


class TruckTelemetry(_Base):
    """GPS/IoT telemetry ping from an in-cab device."""

    ts: datetime = Field(default_factory=_utcnow)
    device_id: str
    plate: Optional[str] = None
    lat: float
    lon: float
    speed_kmh: float = 0.0
    heading: float = 0.0
    battery: Optional[float] = None
    accuracy_m: Optional[float] = None


class TrafficSnapshot(_Base):
    """Aggregated traffic state for a corridor segment from a map provider."""

    ts: datetime = Field(default_factory=_utcnow)
    segment_id: str
    speed_kmh: float
    jam_factor: float = Field(ge=0.0, le=10.0, description="0=free flow, 10=blocked")
    source: str = "unknown"


class Alert(_Base):
    """An operational alert raised by the AI/rules layer."""

    id: UUID = Field(default_factory=uuid4)
    ts: datetime = Field(default_factory=_utcnow)
    kind: str
    severity: str = "info"
    gate_id: Optional[str] = None
    plate: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    ack: bool = False


class Scenario(_Base):
    """A driven demo scenario (e.g. surge, breakdown, weather degradation)."""

    id: str
    name: str
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    params: Dict[str, Any] = Field(default_factory=dict)


# Topic name constants used across services.
TOPIC_ANPR = "anpr.reads"
TOPIC_RFID = "rfid.reads"
TOPIC_TELEMETRY = "truck.telemetry"
TOPIC_TRAFFIC = "traffic.snapshots"
TOPIC_ALERTS = "alerts"

MQTT_RFID_PREFIX = "rfid/readers"  # e.g. rfid/readers/R-01


__all__ = [
    "VehicleClass",
    "AnprRead",
    "VahanRecord",
    "FastagPing",
    "RfidRead",
    "TruckTelemetry",
    "TrafficSnapshot",
    "Alert",
    "Scenario",
    "TOPIC_ANPR",
    "TOPIC_RFID",
    "TOPIC_TELEMETRY",
    "TOPIC_TRAFFIC",
    "TOPIC_ALERTS",
    "MQTT_RFID_PREFIX",
]
