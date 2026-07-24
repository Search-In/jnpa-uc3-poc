"""VESARR (Vessel Arrival) / VESDEP (Vessel Movement) → core.vessel_call_event.

These are the marine-side ACTUALS. Each present timestamp becomes one
``_target='vessel_call_event'`` record, tagged with the call-resolution keys
(``vcn`` / ``via_no`` / ``rotation_no``) — resolving those to a concrete
``call_id`` is a DB concern handled downstream, not here. The parser deliberately
emits a row per milestone so REPEATED event types (e.g. a re-anchoring) are never
collapsed. Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .pcs_common import ft, parse_pcs_dt

# (event_type, source tag) per message. Order = chronological lifecycle order.
_EVENT_MAP: dict[str, tuple[tuple[str, str], ...]] = {
    "VESARR": (
        ("ANCHORED", "AnchoringDateTime"),
        ("PILOT_BOARDED", "PilotBoardedTime"),
        ("BERTHED", "BerthDateTime"),
        ("ARRIVED", "DateTimeArrivalVessel"),
    ),
    "VESDEP": (
        ("PILOT_BOARDED", "PilotBoardedTime"),
        ("DEPARTED", "DateTimeOfDeparture"),
        ("SAILED", "SailingDateTimeOfPort"),
    ),
}


def _parse_movement(root: ET.Element, *, message: str, source_file: Optional[str]) -> list[dict[str, Any]]:
    vcn = ft(root, "VCN")
    via_no = ft(root, "VoyageNumber")
    rotation_no = ft(root, "RotationNumber")
    berth_code = ft(root, "BerthNumber")
    vessel_name = ft(root, "VesselName")
    common_ref = ft(root, "CommonRefNumber")

    records: list[dict[str, Any]] = []
    for event_type, tag in _EVENT_MAP[message]:
        ts = parse_pcs_dt(ft(root, tag))
        if ts is None:
            continue  # absent milestone — skip, don't fabricate
        records.append({
            "_target": "vessel_call_event",
            "_message": message,
            "_source_file": source_file,
            # Call-resolution keys — resolved to call_id downstream (not here).
            "vcn": vcn,
            "via_no": via_no,
            "rotation_no": rotation_no,
            "vessel_name": vessel_name,
            "event_type": event_type,
            "event_ts": ts,
            "berth_code": berth_code,
            "source_note": common_ref,
        })
    return records


def parse_vesarr(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    return _parse_movement(root, message="VESARR", source_file=source_file)


def parse_vesdep(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    return _parse_movement(root, message="VESDEP", source_file=source_file)
