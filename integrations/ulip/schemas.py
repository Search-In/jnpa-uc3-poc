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
from xml.etree import ElementTree

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


def _as_coord(value: Any) -> Optional[float]:
    """One coordinate -> float. GATISHAKTI and LDB both send coordinates as
    high-precision *strings* (``"24.4059522538221785"``); anything unparseable
    degrades to None rather than failing the surrounding record."""
    if value in (None, ""):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


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
           detail: Dict[str, Any], label: Optional[str] = None) -> Dict[str, Any]:
    return {
        "ref_type": ref_type,
        "ref_id": ref_id,
        "event_type": event_type,
        # LDB's own milestone name — PORT IN / PORT OUT / GATE OUT / RAIL OUT.
        # ``event_type`` is the canonical bucket every consumer switches on, so
        # it stays CONTAINER_MOVEMENT; without carrying the label alongside it,
        # a thirteen-leg trail renders as thirteen identical rows and the port
        # milestones — the whole point of tracking a box — are lost.
        "event_label": label,
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
def _trail_is_about(envelope: UlipEnvelope, wanted: str) -> bool:
    """True unless the trail names a container other than the one requested.

    An answer that names no container at all is accepted — some LDB payloads
    omit it — so this only rejects a positive contradiction, never silence.
    """
    for item in _walk_dicts(envelope.response_items()):
        found = _first(item, "cntrno", "containernumber", "containerNumber")
        if found and str(found).strip().upper() != wanted:
            return False
    return True


def normalize_container_events(envelope: UlipEnvelope,
                               container_number: str) -> List[Dict[str, Any]]:
    """Flatten a ULIP LDB answer into CONTAINER_MOVEMENT logistics events.

    LDB movement records vary by leg (rail/road/port); any dict carrying an
    event/activity label or an event timestamp is treated as one movement.
    """
    events: List[Dict[str, Any]] = []
    seen: set = set()
    wanted = str(container_number).strip().upper()
    # LDB staging answers with the SAME static trail whatever container is
    # asked for — TCLU8538808 comes back for CXRU1145597, for NSST1234570 and
    # for NLDSL's own documented sample. Filing another box's port milestones
    # under the one an operator asked about is worse than answering "no data":
    # it would place a container somewhere it has never been. The trail names
    # its own subject in ``cntrDetail.cntrno``, so that is checked once for the
    # whole answer — a per-row check is not enough, because the vessel and
    # expectation blocks carry no container number of their own and would slip
    # through while the movement rows were dropped.
    if not _trail_is_about(envelope, wanted):
        return []
    for item in _walk_dicts(envelope.response_items()):
        # The alias lists must include LDB's own all-lowercase spellings
        # (``eventname``, ``currentlocation``, ``timestamptimezone`` …) — they
        # do NOT match the camelCase aliases, so omitting them made every
        # trackLog entry fail the guard below and vanish.
        label = _first(item, "eventname", "eventCode", "event", "activity",
                       "movementType", "status")
        ts_raw = _first(item, "timestamptimezone", "timetimestamp", "infotime",
                        "eventTime", "actualTime", "eventDate", "timestamp",
                        "time", "actualTimestamp")
        location = _first(item, "currentlocation", "location", "locationName",
                          "place", "currentLocation", "terminal")
        if label is None and ts_raw is None:
            continue
        if location is None and label is None:
            continue
        # ``seqNo`` is LDB's own per-leg ordinal and repeats across trails, so
        # it cannot join the marker — the timestamp/label/location triple is
        # what actually identifies a movement.
        marker = (str(label), str(ts_raw), str(location))
        if marker in seen:
            continue
        seen.add(marker)
        events.append(_event(
            ref_type=REF_TYPE_CONTAINER,
            ref_id=container_number,
            event_type=EVENT_CONTAINER_MOVEMENT,
            label=str(label) if label is not None else None,
            event_ts=parse_ts(ts_raw),
            location=str(location) if location is not None else None,
            latitude=_as_coord(_first(item, "latitude", "lat")),
            longitude=_as_coord(_first(item, "longitude", "lon", "long")),
            source_api="LDB",
            detail=item,
        ))
    events.sort(key=lambda e: e["event_ts"] or "", reverse=True)
    return events


# ------------------------------------------------------- VAHAN (RC) APIs
def _rc_fields(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The RC particulars out of one payload dict, or None if it isn't one.

    Accepts BOTH spellings ULIP emits for the same data: VAHAN/04 answers
    camelCase JSON (``rcRegnNo``) while VAHAN/01·02·03 answer an XML document
    whose elements are snake_case (``rc_regn_no``). Normalising both here is
    what makes the four VAHAN APIs interchangeable upstream.
    """
    regn = _first(item, "rcRegnNo", "rc_regn_no", "registrationNumber")
    if regn is None:
        return None
    return {
        "rc_number": str(regn).strip().upper(),
        "owner_name": _first(item, "rcOwnerName", "rc_owner_name"),
        "vehicle_class": _first(item, "rcVhClassDesc", "rc_vh_class_desc",
                                "rcVchCatgDesc", "rc_vch_catg_desc"),
        "fuel_type": _first(item, "rcFuelDesc", "rc_fuel_desc"),
        "fitness_valid_to": _first(item, "rcFitUpto", "rc_fit_upto"),
        "puc_valid_to": _first(item, "rcPuccUpto", "rc_pucc_upto"),
        "insurance_valid_to": _first(item, "rcInsuranceUpto", "rc_insurance_upto"),
        "registration_date": _first(item, "rcRegnDt", "rc_regn_dt"),
        "state": _first(item, "rcRegisteredAt", "rc_registered_at",
                        "stateCd", "state_cd"),
        "rto_code": _first(item, "rtoCd", "rto_cd"),
        "blacklist_status": _first(item, "rcBlacklistStatus", "rc_blacklist_status"),
        "chassis_number": _first(item, "rcChasiNo", "rc_chasi_no"),
        "engine_number": _first(item, "rcEngNo", "rc_eng_no"),
        "maker_model": _first(item, "rcMakerModel", "rc_maker_model"),
        "status": _first(item, "rcStatus", "rc_status"),
    }


def _find_rc(envelope: "UlipEnvelope") -> Optional[Dict[str, Any]]:
    for item in _walk_dicts(envelope.response_items()):
        fields = _rc_fields(item)
        if fields is not None:
            return fields
    return None


def normalize_rc(envelope: "UlipEnvelope") -> Optional[Dict[str, Any]]:
    """VAHAN/04 (native JSON) -> the flat RC dict, or None when the vehicle is
    unknown.

    A miss is NOT an error at the envelope level: ULIP answers HTTP 200 with
    ``responseStatus: "ERROR"`` and ``message.text: "Vehicle Details not
    Found"`` (code 231), so callers must treat None as "not found" and fall
    through to their next rung rather than surfacing a failure.
    """
    return _find_rc(envelope)


def normalize_vahan_xml(envelope: "UlipEnvelope") -> Optional[Dict[str, Any]]:
    """VAHAN/01·02·03 -> the same flat RC dict as :func:`normalize_rc`.

    These three answer an XML *document serialised as a string* inside the JSON
    envelope's ``response`` key. Parsing is best-effort: a malformed or absent
    document degrades to None (the caller's next rung) rather than raising.
    """
    for item in _walk_dicts(envelope.response_items()):
        raw = item.get("response")
        if not isinstance(raw, str) or "<" not in raw:
            continue
        try:
            root = ElementTree.fromstring(raw.strip())
        except ElementTree.ParseError:
            continue
        flat = {child.tag: (child.text or "").strip() for child in root.iter()}
        fields = _rc_fields(flat)
        if fields is not None:
            return fields
    return None


# ----------------------------------------------------- SARATHI (licence) API
def normalize_dl(envelope: "UlipEnvelope") -> Optional[Dict[str, Any]]:
    """SARATHI/02 -> a flat DL dict, or None when the licence is unknown.

    Like VAHAN, a miss arrives as HTTP 200: ``errorcode: -1`` with
    ``errormessage: "Details not Found For Given DLNumber"`` (SARATHI/02), or a
    ``dldetobj[].erormsg: "No Details are available."`` block (SARATHI/01 —
    note the upstream's single-r spelling). Both yield None.
    """
    for item in _walk_dicts(envelope.response_items()):
        info = item.get("DLinformation")
        if not isinstance(info, dict):
            continue
        covs = info.get("Classofcovs")
        classes: List[str] = []
        if isinstance(covs, list):
            for cov in covs:
                if isinstance(cov, dict):
                    desc = _first(cov, "CovDiscription", "CovDescription", "CovCode")
                    if desc is not None:
                        classes.append(str(desc))
                elif cov not in (None, ""):
                    classes.append(str(cov))
        return {
            "holder_name": _first(info, "DL_Holder_FullName", "DLHolderFullName"),
            "dl_status": _first(info, "DL_status", "DLstatus"),
            "vehicle_classes": classes,
            "transport_valid_to": _first(info, "TransportValidityTodate"),
            "non_transport_valid_to": _first(info, "NonTransportValidityTodate"),
        }
    return _normalize_dl_detail(envelope)


def _normalize_dl_detail(envelope: "UlipEnvelope") -> Optional[Dict[str, Any]]:
    """SARATHI/01's ``dldetobj`` shape -> the same flat DL dict as /02.

    /01 answers a much richer document than /02 and shares none of its field
    names: the licence lives in ``dldetobj[].dlobj`` (``dlLicno``,
    ``dlStatus``, ``dlTrValdtoDt``, ``dlNtValdtoDt``), the classes of vehicle
    in ``dldetobj[].dlcovs[].covdesc``, and the holder in a ``bio*`` block.
    Normalising both to one shape is what lets /01 stand in for /02 wherever
    the caller only needs identity and validity.

    /01 also carries fields /02 does not — the licence issue date, the issuing
    state and RTO, and ``biPhoto``, a base64 JPEG usable for a port pass.

    On the holder's name, note what the live API actually does: the response
    schema NLDSL supplied shows ``bioNatName`` unmasked next to a masked
    ``bioFullName``, but staging masks **both** (``M*H*S*K*M*R* *O*I*``), so
    neither yields a real name. ``bioNatName`` is still preferred, and
    ``_already_masked`` keeps the value as received — masking an
    already-masked string again would destroy the little signal left.

    Dates here are ISO ``YYYY-MM-DD``; /02 sends ``DD-MM-YYYY``, and
    ``parse_date`` accepts both.
    """
    for item in _walk_dicts(envelope.response_items()):
        dl = item.get("dlobj")
        if not isinstance(dl, dict) or not _first(dl, "dlLicno", "dlOldLicno"):
            continue
        classes: List[str] = []
        covs = item.get("dlcovs")
        if isinstance(covs, list):
            for cov in covs:
                if isinstance(cov, dict):
                    desc = _first(cov, "covdesc", "covabbrv")
                    if desc is not None and str(desc).strip():
                        classes.append(str(desc).strip())
        bio = next((b for b in _walk_dicts([item])
                    if isinstance(b, dict) and _first(b, "bioNatName",
                                                      "bioFullName")), {})
        return {
            "holder_name": _first(bio, "bioNatName", "bioFullName"),
            "dl_status": _first(dl, "dlStatus"),
            "vehicle_classes": classes,
            "transport_valid_to": _first(dl, "dlTrValdtoDt", "dlTrValdtoDate"),
            "non_transport_valid_to": _first(dl, "dlNtValdtoDt",
                                             "dlNtValdtoDate"),
            "dl_number": _first(dl, "dlLicno", "dlOldLicno"),
            "date_of_issue": _first(dl, "dlIssuedt", "dlIssueDate"),
            "date_of_birth": _first(bio, "bioDob"),
            "state": _first(dl, "stateName"),
            "rto_code": _first(dl, "dlRtoCode", "dlIssueauth"),
            "photo_base64": _first(bio, "biPhoto"),
        }
    return None


# ------------------------------------------------------- FASTAG/02 (tag) API
def normalize_tag_status(envelope: "UlipEnvelope") -> List[Dict[str, Any]]:
    """FASTAG/02 -> one flat dict per tag registered against the reference.

    The upstream models each tag as a list of ``{"name": …, "value": …}`` pairs
    under ``vehicle.vehicledetails[].detail[]``; this flattens each to
    ``{"tagid": …, "regnumber": …, "tagstatus": …}`` with lower-cased keys. A
    vehicle legitimately carries several tags (re-issues), so the answer is a
    list, ordered as ULIP sent it. An unknown reference yields ``[]``.
    """
    tags: List[Dict[str, Any]] = []
    for item in _walk_dicts(envelope.response_items()):
        details = item.get("detail")
        if not isinstance(details, list):
            continue
        flat = {
            str(pair.get("name", "")).strip().lower(): pair.get("value")
            for pair in details
            if isinstance(pair, dict) and pair.get("name")
        }
        if flat:
            tags.append(flat)
    return tags


# ------------------------------------------------------- GATISHAKTI APIs
def _gs_rows(envelope: "UlipEnvelope") -> List[Dict[str, Any]]:
    """The ``response[].response.data[]`` rows every GATISHAKTI API wraps its
    payload in. An empty ``data`` (the documented "id does not exist" answer)
    yields ``[]`` — a legitimate empty result, not a failure."""
    rows: List[Dict[str, Any]] = []
    for item in _walk_dicts(envelope.response_items()):
        data = item.get("data")
        if isinstance(data, list):
            rows.extend(row for row in data if isinstance(row, dict))
    return rows


def normalize_toll_plazas(envelope: "UlipEnvelope",
                          state_id: Any) -> List[Dict[str, Any]]:
    """GATISHAKTI/04 -> NHAI toll-plaza rows keyed for ``core.gs_toll_plaza``.

    The live field names are nothing like the integration document's generic
    ``vname``/``lat``/``lon`` sample — a real row is::

        {"plaza_name": "Nandgaon Peth", "tollplazal": 20.951008,
         "tollplaz_1": 77.788359, "nooflanes": "4L", "nhno_new": "NH- 53",
         "nearesthos": null, "tollcollec": null,
         "project_na": "Fagne-MH/GJ Border"}

    ``tollplazal`` / ``tollplaz_1`` are the latitude and longitude, truncated
    to ten characters by whatever shapefile export produced them. Matching only
    the documented names returned **zero plazas for every state** — verified
    against staging on 2026-08-11 — so the live spellings lead the alias lists.
    """
    plazas: List[Dict[str, Any]] = []
    for row in _gs_rows(envelope):
        name = _first(row, "plaza_name", "vname", "name",
                      "tollPlazaName", "plazaName")
        if name is None:
            continue
        plazas.append({
            "state_id": str(state_id),
            "name": str(name),
            "nh_no": _first(row, "nhno_new", "nhno", "nh_no", "nhNumber"),
            "latitude": _as_coord(_first(row, "tollplazal", "lat", "latitude")),
            "longitude": _as_coord(_first(row, "tollplaz_1", "lon", "long",
                                          "longitude")),
            "detail": row,
        })
    return plazas


def normalize_road_network(envelope: "UlipEnvelope",
                           **keys: Any) -> List[Dict[str, Any]]:
    """GATISHAKTI/01·02·03 -> reference rows keyed for ``core.gs_road_segment``
    / ``core.gs_road_point``. ``keys`` (``state_id`` / ``nh_no``) is stamped
    onto every row so the caller's query parameter survives into the table.

    What these three APIs actually return on staging is NOT what the
    integration document's ``vname``/``lat``/``lon`` sample suggests, and the
    three do not share a schema (verified 2026-08-11):

    * ``GATISHAKTI/01`` — highway segments, ``road_name`` / ``road_type`` /
      ``lane_statu`` / ``gis_length`` / ``state_ut``. **No coordinates at
      all**, so these rows carry attributes but no geometry.
    * ``GATISHAKTI/02`` — food-storage depots, ``infrastr_n`` / ``infrastr_a``
      / ``storage_ca`` / ``type_infra``. Warehousing infrastructure, not a
      road network.
    * ``GATISHAKTI/03`` — industrial parks, ``park_name`` / ``vname`` /
      ``dist_name`` / ``land_cat`` with ``lat`` / ``lon``.

    Every row is kept whole in ``detail`` precisely because the payloads differ
    this much; ``name`` picks the best label each schema offers, and rows
    without coordinates are stored with nulls rather than dropped.
    """
    rows: List[Dict[str, Any]] = []
    for row in _gs_rows(envelope):
        rows.append({
            **{k: (str(v) if v is not None else None) for k, v in keys.items()},
            "name": _first(row, "road_name", "park_name", "infrastr_n",
                           "vname", "name", "roadName"),
            "latitude": _as_coord(_first(row, "lat", "latitude")),
            "longitude": _as_coord(_first(row, "lon", "long", "longitude")),
            "detail": row,
        })
    return rows


__all__ = [
    "UlipEnvelope",
    "normalize_vehicle_events",
    "normalize_container_events",
    "normalize_rc",
    "normalize_vahan_xml",
    "normalize_dl",
    "normalize_tag_status",
    "normalize_toll_plazas",
    "normalize_road_network",
    "parse_ts",
    "parse_geocode",
    "REF_TYPE_VEHICLE",
    "REF_TYPE_CONTAINER",
    "EVENT_TOLL_CROSSING",
    "EVENT_CONTAINER_MOVEMENT",
]
