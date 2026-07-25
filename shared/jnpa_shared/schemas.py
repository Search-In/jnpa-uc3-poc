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


class Condition(str, Enum):
    """Visibility/illumination condition under which an ANPR read was taken.

    Drives the OCR-confidence distribution: CLEAR yields the headline ≥95%
    accuracy; FOG/NIGHT degrade gracefully so the presenter can show
    low-confidence flagging. See :func:`ocr_confidence_for_condition`.
    """

    CLEAR = "CLEAR"
    DUST = "DUST"
    FOG = "FOG"
    NIGHT = "NIGHT"


# Per-condition OCR confidence distributions as (mean, stddev). CLEAR sits well
# above the 95% target; FOG/NIGHT degrade so low-confidence flagging is visible.
OCR_CONFIDENCE_BY_CONDITION: dict[str, tuple[float, float]] = {
    Condition.CLEAR.value: (0.972, 0.015),
    Condition.DUST.value: (0.935, 0.030),
    Condition.FOG.value: (0.870, 0.060),
    Condition.NIGHT.value: (0.845, 0.070),
}

# Below this confidence a read is flagged low-confidence for human review.
OCR_LOW_CONF_THRESHOLD = 0.90


def ocr_confidence_for_condition(condition: str, rng) -> float:
    """Sample a recognition confidence for ``condition`` using ``rng``.

    ``rng`` is a ``random.Random`` (seed it from ``settings.derive_seed`` for
    deterministic replay). The result is clamped to ``[0, 1]``.
    """
    mean, sd = OCR_CONFIDENCE_BY_CONDITION.get(
        condition, OCR_CONFIDENCE_BY_CONDITION[Condition.CLEAR.value]
    )
    return max(0.0, min(1.0, rng.gauss(mean, sd)))


class AnprRead(_Base):
    """Automatic Number Plate Recognition read from a camera."""

    ts: datetime = Field(default_factory=_utcnow)
    camera_id: str
    plate: str
    conf: float = Field(ge=0.0, le=1.0, description="recognition confidence 0..1")
    vehicle_class: VehicleClass = VehicleClass.UNKNOWN
    image_url: Optional[str] = None
    weather: Optional[str] = None
    # Visibility condition driving the OCR-confidence distribution. Defaults to
    # CLEAR so existing callers (which don't set it) are unaffected.
    condition: Condition = Condition.CLEAR
    degraded: bool = False

    @property
    def low_confidence(self) -> bool:
        """True when the read should be flagged for human review."""
        return self.conf < OCR_LOW_CONF_THRESHOLD


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
    compatibility with the ``core.vehicle_rc`` writeback path; new code
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

    # --- Legacy / writeback compatibility fields (core.vehicle_rc) ---
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
    """A row in ``core.ulip_service`` — how a service advertises itself for the
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


class TelemetrySource(str, Enum):
    """Provenance of a truck position, i.e. which fallback rung produced it.

    The Trucking-App fallback chain (bid §8.5.3) degrades
    ``APP_GPS → ULIP_RELAY → WEB_CHECKIN``; each rung still moves the vehicle,
    so the dashboard shows reduced fidelity rather than a blind spot.
    """

    APP_GPS = "APP_GPS"
    ULIP_RELAY = "ULIP_RELAY"
    WEB_CHECKIN = "WEB_CHECKIN"


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
    # Which fallback rung produced this ping. Defaults to APP_GPS (primary) so
    # existing producers are unaffected.
    source: TelemetrySource = TelemetrySource.APP_GPS


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
# Canonical UC-III event types (added to bring the simulators to the demo
# standard). Each models one Appendix-C feed/event from the simulator prompt.
# All carry timezone-aware UTC timestamps and ride the event backbone wrapped
# in a CloudEvents envelope (see jnpa_shared.cloudevents).
# ---------------------------------------------------------------------------


class TrackBehaviour(str, Enum):
    NORMAL = "NORMAL"
    WRONG_WAY = "WRONG_WAY"
    ABANDONED = "ABANDONED"
    ILLEGAL_PARKING = "ILLEGAL_PARKING"
    ROUTE_DEVIATION = "ROUTE_DEVIATION"


class VehicleTrack(_Base):
    """A ByteTrack vehicle trajectory with an autoencoder-derived behaviour."""

    ts: datetime = Field(default_factory=_utcnow)
    track_id: str
    camera_id: str
    # Each trajectory point is {ts, lat, lon} (or pixel x/y); kept loose so the
    # tracker can emit either world or image coordinates.
    trajectory: list[Dict[str, Any]] = Field(default_factory=list)
    behaviour: TrackBehaviour = TrackBehaviour.NORMAL
    anomaly_score: float = Field(default=0.0, ge=0.0, le=1.0)


class FastTagTxn(_Base):
    """A FASTag (NETC) plaza transaction — corridor entry / toll debit.

    Distinct from :class:`FastagPing` (a balance/status *lookup*): this is a
    plaza *crossing* event with an amount debited.
    """

    ts: datetime = Field(default_factory=_utcnow)
    tag_id: str
    plaza_id: str
    vehicle_no: Optional[str] = None
    amount: float = 0.0
    balance: Optional[float] = None


class GateDirection(str, Enum):
    IN = "IN"
    OUT = "OUT"


class GateTransaction(_Base):
    """A gate crossing (boom-to-boom), feeding Gate Queue Wait / Txn Time KPIs."""

    gate_id: str
    direction: GateDirection
    vehicle_no: str
    container_no: Optional[str] = None
    start_ts: datetime = Field(default_factory=_utcnow)
    end_ts: Optional[datetime] = None
    outcome: str = "CLEARED"   # CLEARED | REJECTED | RESCHEDULED

    @property
    def duration_s(self) -> Optional[float]:
        if self.end_ts is None:
            return None
        return (self.end_ts - self.start_ts).total_seconds()


class ParkingState(_Base):
    """Point-in-time availability for a parking facility (CPP or a lot)."""

    ts: datetime = Field(default_factory=_utcnow)
    facility_id: str
    capacity: int = Field(ge=0)
    occupied: int = Field(ge=0)

    @property
    def available_slots(self) -> int:
        return max(0, self.capacity - self.occupied)


class WeighbridgeReading(_Base):
    """A weighbridge gross-weight reading (Auto-LEO gate data)."""

    ts: datetime = Field(default_factory=_utcnow)
    wb_id: str
    vehicle_no: str
    gross_wt_kg: float = Field(ge=0.0)


class EmptyContainerMove(_Base):
    """An empty-container movement to/from an ECD (TRT-empty-from-ECD KPI)."""

    container_no: str
    ecd_id: str
    out_ts: Optional[datetime] = None
    in_ts: Optional[datetime] = None


class CarbonRecord(_Base):
    """Per-trip CO2e estimate feeding the carbon tile."""

    ts: datetime = Field(default_factory=_utcnow)
    vehicle_no: str
    trip_id: str
    distance_km: float = Field(ge=0.0)
    emissions_kg_co2: float = Field(ge=0.0)


class FaceVerification(_Base):
    """Driver face-verification result — SYNTHETIC/consented faces only (DPDP).

    ``driver_id`` is a synthetic identifier; no real biometric template is ever
    carried on the wire. See ASSUMPTIONS.md.
    """

    ts: datetime = Field(default_factory=_utcnow)
    driver_id: str
    gate_id: str
    match_score: float = Field(ge=0.0, le=1.0)
    result: str = "MATCH"   # MATCH | NO_MATCH | PROVISIONAL
    synthetic: bool = True


class GeofenceType(str, Enum):
    NO_PARKING = "NO_PARKING"
    RESTRICTED = "RESTRICTED"


class GeofenceViolation(_Base):
    """A geofence breach (no-parking / restricted zone)."""

    ts: datetime = Field(default_factory=_utcnow)
    vehicle_no: str
    zone_id: str
    type: GeofenceType = GeofenceType.NO_PARKING
    enter_ts: datetime = Field(default_factory=_utcnow)
    duration_s: float = Field(default=0.0, ge=0.0)


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


# --- Cross-twin contract (XT-1): defined ONCE in the shared package so UC-II
# (producer) and UC-III (consumer) share one typed schema. UC-II publishes a DPD
# (Direct Port Delivery) release spike; UC-III consumes it as leading-indicator
# truck demand (see scenarios/uc2_bridge.py + TFC-3).
class DpdReleaseEvent(_Base):
    """UC-II -> UC-III DPD release-spike event (topic ``cargo.dpd_release``)."""

    dpd_release_spike: float = Field(default=1.0, ge=0.0, description="multiplier vs 1.0x baseline")
    window_min: int = Field(default=40, ge=1, description="release window in minutes")
    ts: datetime = Field(default_factory=_utcnow)


# Topic name constants used across services.
TOPIC_ANPR = "anpr.reads"
TOPIC_RFID = "rfid.reads"
TOPIC_TELEMETRY = "truck.telemetry"
TOPIC_TRAFFIC = "traffic.snapshots"
TOPIC_ALERTS = "alerts"

# Newly event-sourced feeds (Phase C). Services that previously answered only
# over HTTP now also publish onto these topics (tagged sourcesystem=SIM) so the
# dashboard cannot tell SIM from LIVE except via the mode badge.
TOPIC_VEHICLE_TRACK = "vehicle.tracks"
TOPIC_FASTAG_TXN = "fastag.txns"
TOPIC_GATE_TXN = "gate.transactions"
TOPIC_PARKING = "parking.state"
TOPIC_WEIGHBRIDGE = "weighbridge.reads"
TOPIC_EMPTY_CONTAINER = "empty.container.moves"
TOPIC_CARBON = "carbon.records"
TOPIC_FACE = "face.verifications"
TOPIC_GEOFENCE = "geofence.violations"
TOPIC_DPD_RELEASE = "cargo.dpd_release"  # cross-twin (UC-II -> UC-III), XT-1/XT-2

MQTT_RFID_PREFIX = "rfid/readers"  # e.g. rfid/readers/R-01


__all__ = [
    "VehicleClass",
    "BlacklistStatus",
    "FastagStatus",
    "Condition",
    "OCR_CONFIDENCE_BY_CONDITION",
    "OCR_LOW_CONF_THRESHOLD",
    "ocr_confidence_for_condition",
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
    "TelemetrySource",
    "TruckTelemetry",
    "TrafficSnapshot",
    "Alert",
    "Scenario",
    # --- canonical UC-III event types ---
    "TrackBehaviour",
    "VehicleTrack",
    "FastTagTxn",
    "GateDirection",
    "GateTransaction",
    "ParkingState",
    "WeighbridgeReading",
    "EmptyContainerMove",
    "CarbonRecord",
    "FaceVerification",
    "GeofenceType",
    "GeofenceViolation",
    "DpdReleaseEvent",
    # --- topics ---
    "TOPIC_ANPR",
    "TOPIC_RFID",
    "TOPIC_TELEMETRY",
    "TOPIC_TRAFFIC",
    "TOPIC_ALERTS",
    "TOPIC_VEHICLE_TRACK",
    "TOPIC_FASTAG_TXN",
    "TOPIC_GATE_TXN",
    "TOPIC_PARKING",
    "TOPIC_WEIGHBRIDGE",
    "TOPIC_EMPTY_CONTAINER",
    "TOPIC_CARBON",
    "TOPIC_FACE",
    "TOPIC_GEOFENCE",
    "TOPIC_DPD_RELEASE",
    "MQTT_RFID_PREFIX",
]
