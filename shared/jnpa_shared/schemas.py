"""Pydantic v2 event/record models for the JNPA UC-III PoC.

These models are the wire contract for Kafka/MQTT messages and the row shape
for the corresponding Postgres tables. All timestamps are timezone-aware UTC.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class BlacklistStatus(str, Enum):
    CLEAR = "CLEAR"
    BLACKLISTED = "BLACKLISTED"


class FastagStatus(str, Enum):
    ACTIVE = "ACTIVE"
    LOW_BALANCE = "LOW_BALANCE"
    BLACKLISTED = "BLACKLISTED"
    INACTIVE = "INACTIVE"


class VahanRecord(_Base):
    """Vehicle registration record sourced from Vahan (Parivahan) / Surepass.

    Field set mirrors the Parivahan RC schema (see ``ingest/vahan_sim``). The
    legacy fields (``rc_type``, ``owner_hash`` …) are kept for backward
    compatibility with the ``jnpa.vehicle_master`` writeback path; new code
    should prefer the canonical fields below.
    """

    # --- Canonical Parivahan RC fields ---
    # `rc_number` is the canonical plate; it mirrors the legacy `plate` field
    # below (a model validator backfills whichever is missing) so existing
    # callers that pass `plate=` keep working.
    rc_number: Optional[str] = Field(
        default=None, description="registration / plate number e.g. MH04AB1234"
    )
    owner_name_masked: Optional[str] = None
    vehicle_class: Optional[str] = None
    fuel_type: Optional[str] = None
    fitness_valid_to: Optional[date] = None
    puc_valid_to: Optional[date] = None
    insurance_valid_to: Optional[date] = None
    registration_date: Optional[date] = None
    state: Optional[str] = None
    rto_code: Optional[str] = None
    blacklist_status: BlacklistStatus = BlacklistStatus.CLEAR

    # --- Legacy / writeback compatibility fields (jnpa.vehicle_master) ---
    plate: Optional[str] = None
    rc_type: Optional[str] = None
    owner_hash: Optional[str] = None
    fastag_status: Optional[str] = None
    provisional: bool = False
    provisional_until: Optional[datetime] = None

    @model_validator(mode="after")
    def _mirror_plate(self) -> "VahanRecord":
        """Keep ``rc_number`` and ``plate`` in sync; at least one is required."""
        if self.rc_number and not self.plate:
            self.plate = self.rc_number
        elif self.plate and not self.rc_number:
            self.rc_number = self.plate
        if not self.rc_number:
            raise ValueError("VahanRecord requires rc_number (or plate)")
        return self

    @property
    def plate_number(self) -> str:
        """Canonical plate accessor (``plate`` falls back to ``rc_number``)."""
        return self.plate or self.rc_number or ""


class SarathiRecord(_Base):
    """Driving-licence record sourced from Sarathi (Parivahan) / Surepass."""

    dl_number: str
    holder_name_masked: Optional[str] = None
    date_of_issue: Optional[date] = None
    valid_to: Optional[date] = None
    vehicle_classes: list[str] = Field(default_factory=list)
    state: Optional[str] = None
    rto_code: Optional[str] = None
    blacklist_status: BlacklistStatus = BlacklistStatus.CLEAR


class FastagPing(_Base):
    """A FASTag (NETC) balance/status reading for a vehicle.

    Carries the toll/gantry read fields (``tag_id``/``reader_id`` …) used by
    the corridor pipeline plus the NETC balance/status fields returned by the
    Vahan/FASTag lookup surface.
    """

    ts: datetime = Field(default_factory=_utcnow)
    plate: Optional[str] = None
    tag_id: Optional[str] = None
    reader_id: str = "lookup"
    plaza: Optional[str] = None
    bank: Optional[str] = None
    balance: Optional[float] = None
    status: FastagStatus = FastagStatus.ACTIVE


class ServiceRegistration(_Base):
    """A row in ``jnpa.services`` — how a service advertises itself for the
    fallback orchestrator (Prompt 4) to discover sim vs. live endpoints."""

    name: str                       # logical service e.g. "vahan"
    kind: str                       # "sim" | "live"
    base_url: str                   # reachable on the jnpa network
    healthy: bool = True
    enabled: bool = True
    registered_at: datetime = Field(default_factory=_utcnow)
    meta: Dict[str, Any] = Field(default_factory=dict)


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


# ---------------------------------------------------------------------------
# Indian plate / DL helpers (shared by the Vahan simulator + live adapter)
# ---------------------------------------------------------------------------
import re

# Classic series:  SS DD L[L] NNNN   e.g. MH04AB1234, MH43A1234, GJ01AAA1234
# (1-2 series letters, 1-4 final digits per RTO practice).
# BH series:       YY BH NNNN LL      e.g. 22BH1234AA
_PLATE_CLASSIC = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{1,4}$")
_PLATE_BH = re.compile(r"^\d{2}BH\d{4}[A-Z]{1,2}$")
_DL_RE = re.compile(r"^[A-Z]{2}\d{2}\s?\d{11}$")


def normalize_plate(plate: str) -> str:
    """Upper-case and strip spaces/hyphens so ``MH-04-AB-1234`` == ``MH04AB1234``."""
    return re.sub(r"[\s\-]", "", plate or "").upper()


def is_valid_plate(plate: str) -> bool:
    """True for a classic Indian registration plate or a new BH-series plate."""
    p = normalize_plate(plate)
    return bool(_PLATE_CLASSIC.match(p) or _PLATE_BH.match(p))


def is_valid_dl(dl_number: str) -> bool:
    """True for a plausible Indian driving-licence number (SS RR NNNNNNNNNNN)."""
    return bool(_DL_RE.match((dl_number or "").strip().upper()))


def mask_owner_name(name: str) -> str:
    """PII-safe masking: keep first char of each token, star the rest.

    ``RAJESH KUMAR`` -> ``R***** K****``. Single-char tokens are left as-is.
    """
    parts = [p for p in (name or "").split() if p]
    out = []
    for p in parts:
        out.append(p[0] + ("*" * (len(p) - 1)) if len(p) > 1 else p)
    return " ".join(out)


# Topic name constants used across services.
TOPIC_ANPR = "anpr.reads"
TOPIC_RFID = "rfid.reads"
TOPIC_TELEMETRY = "truck.telemetry"
TOPIC_TRAFFIC = "traffic.snapshots"
TOPIC_ALERTS = "alerts"

MQTT_RFID_PREFIX = "rfid/readers"  # e.g. rfid/readers/R-01


__all__ = [
    "VehicleClass",
    "BlacklistStatus",
    "FastagStatus",
    "AnprRead",
    "VahanRecord",
    "SarathiRecord",
    "FastagPing",
    "ServiceRegistration",
    "is_valid_plate",
    "is_valid_dl",
    "normalize_plate",
    "mask_owner_name",
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
