"""CHPOI03 (IGM — Import General Manifest) parser.

Streaming decoder: an IGM file is up to ~5.5 MB with ~2 800 containers across
~1 200 cargo lines, so we use ``ElementTree.iterparse`` and ``.clear()`` each
``<cargo>`` subtree after extracting it — memory stays bounded regardless of
manifest size (large-manifest support, per the tender).

Shape:  CHPOI03Payload / DocumentDetails / vesinfo (vessel+IGM header)
                                            -> cargo (line)  [1:N]
                                                 -> container [1:N]
"""
from __future__ import annotations

from typing import Any, Optional

from jnpa_shared.iso6346 import is_valid_container_no

from .common import (
    CustomsParseError,
    ParsedMessage,
    clean,
    parse_ddmmyyyy,
    parse_ddmmyyyy_time,
    parse_document_header,
    to_int,
    to_num,
)


def _container(el: Any, line_no: Optional[int], subline_no: int) -> dict[str, Any]:
    cn = clean(el.findtext("ContainerNo"))
    return {
        "container_no": cn,
        "iso_valid": bool(cn) and is_valid_container_no(cn),
        "line_no": line_no,
        "subline_no": subline_no,
        "seal_no": clean(el.findtext("ContainerSealNo")),
        "container_agent_code": clean(el.findtext("ContainerAgentCode")),
        "container_status": clean(el.findtext("ContainerStatus")),
        "no_of_packages": to_int(el.findtext("TotalNoofPackagesinContainer")),
        "container_weight": to_num(el.findtext("Containerweight")),
        "iso_size_type": clean(el.findtext("ISOCode")),
        "soc_flag": clean(el.findtext("SOCFlag")),
    }


def _cargo_line(el: Any) -> dict[str, Any]:
    line_no = to_int(el.findtext("LineNo"))
    subline_no = to_int(el.findtext("SublineNo")) or 0
    # Importer address is nested: <Address1><Address1>street</Address1><Address2>state</Address2>...>.
    addr = el.find("Address1")
    importer_address = clean(addr.findtext("Address1")) if addr is not None else None
    importer_state = clean(addr.findtext("Address2")) if addr is not None else None
    return {
        "line_no": line_no,
        "subline_no": subline_no,
        "bl_no": clean(el.findtext("BLNo")),
        "bl_date": parse_ddmmyyyy(el.findtext("BLDate")),
        "house_bl_no": clean(el.findtext("HouseBLNo")),
        "house_bl_date": parse_ddmmyyyy(el.findtext("HouseBLDate")),
        "port_of_loading": clean(el.findtext("PortofLoading")),
        "port_of_destination": clean(el.findtext("PortofDestination")),
        "port_of_discharge": clean(el.findtext("PortofDischarge")),
        "importer_name": clean(el.findtext("ImportersName")),
        "importer_address": importer_address,
        "importer_state": importer_state,
        "notified_party": clean(el.findtext("NameofanyotherNotifiedParty")),
        "nature_of_cargo": clean(el.findtext("NatureofCargo")),
        "item_type": clean(el.findtext("ItemType")),
        "cargo_movement": clean(el.findtext("CargoMovement")),
        "no_of_packages": to_int(el.findtext("NumberofPackages")),
        "type_of_packages": clean(el.findtext("TypeofPackages")),
        "gross_weight": to_num(el.findtext("Grossweight")),
        "unit_of_weight": clean(el.findtext("UnitofWeight")),
        "goods_description": clean(el.findtext("GoodsDescription")),
        "mlo_code": clean(el.findtext("MLOCode")),
        "be_regularised": clean(el.findtext("BE_REGULARISED")),
        "containers": [_container(c, line_no, subline_no) for c in el.findall("container")],
    }


def _vessel(el: Any) -> dict[str, Any]:
    return {
        "customs_house_code": clean(el.findtext("CustomsHouseCode")),
        "igm_no": clean(el.findtext("IGM_NO")),
        "igm_date": parse_ddmmyyyy(el.findtext("IGM_DT")),
        "imo_code": clean(el.findtext("IMOCodeofVessel")),
        "vessel_code": clean(el.findtext("VesselCode")),
        "voyage_no": clean(el.findtext("VoyageNo")),
        "shipping_line_code": clean(el.findtext("ShippingLineCode")),
        "shipping_agent_code": clean(el.findtext("ShipingAgentCode")),  # source-spelled tag
        "master_name": clean(el.findtext("MasterName")),
        "port_of_arrival": clean(el.findtext("PortofArrival")),
        "vessel_type": clean(el.findtext("Vesseltype")),
        "total_no_of_lines": to_int(el.findtext("TotalNoofLines")),
        "brief_cargo_desc": clean(el.findtext("BriefCargoDescription")),
        "expected_arrival": parse_ddmmyyyy_time(el.findtext("ExpectedDateandtimeofArrival")),
        "entry_inward": parse_ddmmyyyy_time(el.findtext("EntryinwardDateandTime")),
        "terminal_operator_code": clean(el.findtext("TerminalOperatorCode")),
        "lines": [],
    }


def parse_chpoi03(path: str) -> ParsedMessage:
    """Parse an IGM (CHPOI03) XML file at ``path`` into a :class:`ParsedMessage`.

    ``payload = {"vessels": [ {vessel..., "lines": [ {line..., "containers": [...] } ]} ]}``.
    ``record_count`` is the total container count (the leaf join unit)."""
    import xml.etree.ElementTree as ET

    header: dict[str, Any] = {}
    vessels: list[dict[str, Any]] = []
    current_lines: list[dict[str, Any]] = []
    total_containers = 0

    try:
        ctx = ET.iterparse(path, events=("start", "end"))
        root_checked = False
        for event, el in ctx:
            if event == "start" and not root_checked:
                root_checked = True
                if el.tag != "CHPOI03Payload":
                    raise CustomsParseError(
                        f"not a CHPOI03 payload (root=<{el.tag}>): {path}")
                continue
            if event != "end":
                continue
            if el.tag == "DocumentHeader":
                header = parse_document_header(el)
                el.clear()
            elif el.tag == "cargo":
                current_lines.append(_cargo_line(el))
                el.clear()  # free the line+container subtree
            elif el.tag == "vesinfo":
                v = _vessel(el)
                v["lines"] = current_lines
                total_containers += sum(len(ln["containers"]) for ln in current_lines)
                vessels.append(v)
                current_lines = []
                el.clear()
    except ET.ParseError as exc:
        raise CustomsParseError(f"invalid XML in {path}: {exc}") from exc

    if not root_checked:
        raise CustomsParseError(f"empty/unreadable XML: {path}")

    primary_ref = vessels[0]["igm_no"] if vessels else None
    message = {
        "message_type": "CHPOI03",
        "module": "IGM",
        "primary_ref": primary_ref,
        **header,
    }
    return ParsedMessage(message=message, payload={"vessels": vessels},
                         record_count=total_containers)
