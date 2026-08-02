"""Pydantic views over the raw ULIP API responses + normalisation.

Every ULIP API answers inside one common envelope::

    {"error": "false", "code": "200", "message": "SUCCESS", "response": [...]}

Validation is deliberately tolerant (``extra="ignore"``, every field Optional,
mixed str/bool/int ``error``/``code`` accepted) in the same spirit as
:mod:`integrations.tomtom.schemas`: ULIP fronts many source systems (FASTag /
NPCI, LDB, VAHAN, …) whose payload shapes drift — a partial answer must
degrade a field to ``null``, never fail the whole request. What IS enforced:
the body is a JSON object carrying the envelope keys; an envelope that
reports an API-level error is surfaced by the client as
:class:`~integrations.ulip.exceptions.UlipInvalidResponse`.

``normalize_vehicle_events()`` / ``normalize_container_events()`` flatten the
per-API payloads into the compact ``logistics event`` dicts the backend
consumes — the raw ULIP shape is NEVER exposed past this module (it is only
preserved verbatim in each event's ``detail`` and in the audit table).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict

REF_TYPE_VEHICLE = "VEHICLE"
REF_TYPE_CONTAINER = "CONTAINER"

EVENT_TOLL_CROSSING = "TOLL_CROSSING"
EVENT_CONTAINER_MOVEMENT = "CONTAINER_MOVEMENT"

# Accepted upstream timestamp formats (ULIP mixes source-system conventions;
# same tolerance as jnpa_shared.fastag._TS_FORMATS + the trailing-.0 variant
# NPCI reader timestamps carry).
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def parse_ts(value: Any) -> Optional[str]:
    """Best-effort upstream timestamp -> UTC ISO-8601 string (None-safe).

    ULIP timestamps carry no zone marker and are IST wall-clock in practice,
    but guessing an offset would corrupt the audit trail — the value is kept
    naive-as-UTC-formatted and the raw string survives in ``detail``.
    """
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:  # ISO first (covers zone-aware strings)
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def parse_geocode(value: Any) -> Tuple[Optional[float], Optional[float]]:
    """``"lat,lon"`` -> (lat, lon) floats; anything else -> (None, None)."""
    if not isinstance(value, str) or "," not in value:
        return None, None
    lat_s, _, lon_s = value.partition(",")
    try:
        return float(lat_s.strip()), float(lon_s.strip())
    except ValueError:
        return None, None


class UlipEnvelope(BaseModel):
    """The common ULIP answer envelope (tolerant — see module docstring)."""

    model_config = ConfigDict(extra="ignore")

    error: Optional[Any] = None
    code: Optional[Any] = None
    message: Optional[str] = None
    response: Optional[Any] = None

    @property
    def ok(self) -> bool:
        """True when the envelope reports success. ULIP serialises ``error``
        as the *string* "false"/"true" (sometimes a real bool) and ``code`` as
        "200" (sometimes an int) — both spellings are accepted."""
        err = self.error
        if isinstance(err, str):
            err = err.strip().lower() == "true"
        if err:
            return False
        if self.code is None:
            return self.response is not None
        return str(self.code).strip() == "200"

    def response_items(self) -> List[Dict[str, Any]]:
        """The ``response`` block as a list of dicts, whatever ULIP sent."""
        if isinstance(self.response, dict):
            return [self.response]
        if isinstance(self.response, list):
            return [item for item in self.response if isinstance(item, dict)]
        return []


# ------------------------------------------------------------------- helpers
def _first(item: Dict[str, Any], *keys: str) -> Any:
    """First present, non-empty value among ``keys`` (case-insensitive)."""
    lowered = {k.lower(): v for k, v in item.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _walk_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    """Depth-first over every dict inside an arbitrarily nested payload."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def _event(*, ref_type: str, ref_id: str, event_type: str, event_ts: Optional[str],
           location: Optional[str], latitude: Optional[float],
           longitude: Optional[float], source_api: str,
           detail: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref_type": ref_type,
        "ref_id": ref_id,
        "event_type": event_type,
        "event_ts": event_ts,
        "location": location,
        "latitude": latitude,
        "longitude": longitude,
        "source_api": source_api,
        "detail": detail,
    }


# ------------------------------------------------------ FASTAG (vehicle) API
def normalize_vehicle_events(envelope: UlipEnvelope,
                             vehicle_number: str) -> List[Dict[str, Any]]:
    """Flatten a ULIP FASTAG answer into TOLL_CROSSING logistics events.

    The documented shape nests the crossings at
    ``response[].response.vehicle.vehltxnList.txn[]`` — but source-system
    drift is expected, so any dict carrying a reader-read timestamp or a toll
    plaza name anywhere in the payload is treated as one crossing.
    """
    events: List[Dict[str, Any]] = []
    seen: set = set()
    for item in _walk_dicts(envelope.response_items()):
        ts_raw = _first(item, "readerReadTime", "txnTime", "transactionDateTime",
                        "transaction_date_time")
        plaza = _first(item, "tollPlazaName", "toll_plaza_name", "plazaName")
        if ts_raw is None and plaza is None:
            continue
        lat, lon = parse_geocode(_first(item, "tollPlazaGeocode",
                                        "toll_plaza_geocode", "geocode"))
        marker = (str(ts_raw), str(plaza), _first(item, "seqNo", "seq_no"))
        if marker in seen:  # the same txn dict reachable via two walk paths
            continue
        seen.add(marker)
        events.append(_event(
            ref_type=REF_TYPE_VEHICLE,
            ref_id=vehicle_number,
            event_type=EVENT_TOLL_CROSSING,
            event_ts=parse_ts(ts_raw),
            location=str(plaza) if plaza is not None else None,
            latitude=lat,
            longitude=lon,
            source_api="FASTAG",
            detail=item,
        ))
    events.sort(key=lambda e: e["event_ts"] or "", reverse=True)
    return events


# ------------------------------------------------------ LDB (container) API
def normalize_container_events(envelope: UlipEnvelope,
                               container_number: str) -> List[Dict[str, Any]]:
    """Flatten a ULIP LDB answer into CONTAINER_MOVEMENT logistics events.

    LDB movement records vary by leg (rail/road/port); any dict carrying an
    event/activity label or an event timestamp is treated as one movement.
    """
    events: List[Dict[str, Any]] = []
    seen: set = set()
    for item in _walk_dicts(envelope.response_items()):
        label = _first(item, "eventCode", "event", "activity", "movementType",
                       "status")
        ts_raw = _first(item, "eventTime", "actualTime", "eventDate",
                        "timestamp", "time", "actualTimestamp")
        location = _first(item, "location", "locationName", "place",
                          "currentLocation", "terminal")
        if label is None and ts_raw is None:
            continue
        if location is None and label is None:
            continue
        marker = (str(label), str(ts_raw), str(location))
        if marker in seen:
            continue
        seen.add(marker)
        events.append(_event(
            ref_type=REF_TYPE_CONTAINER,
            ref_id=container_number,
            event_type=EVENT_CONTAINER_MOVEMENT,
            event_ts=parse_ts(ts_raw),
            location=str(location) if location is not None else None,
            latitude=None,
            longitude=None,
            source_api="LDB",
            detail=item,
        ))
    events.sort(key=lambda e: e["event_ts"] or "", reverse=True)
    return events


__all__ = [
    "UlipEnvelope",
    "normalize_vehicle_events",
    "normalize_container_events",
    "parse_ts",
    "parse_geocode",
    "REF_TYPE_VEHICLE",
    "REF_TYPE_CONTAINER",
    "EVENT_TOLL_CROSSING",
    "EVENT_CONTAINER_MOVEMENT",
]
