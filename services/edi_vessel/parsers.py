"""COARRI / COPRAR XML parsers — live-format faithful.

Shapes verified against the live dt.jnpa.in corpus (2026-08-06):

COARRI (``COARRI_LOAD_05082026230900.xml``)::

    <ContLoadingNDischargeOder>
      <DocumentHeader><DocumentReference>
        <DocumentType>COARRI</DocumentType> <DocumentNumber>…</DocumentNumber>
        <CommonRefNumber>…</CommonRefNumber> <SenderID>…</SenderID>
      </DocumentReference>…</DocumentHeader>
      <DocumentDetails>
        <COARRIHeader><VCN>…</VCN><TOCode>…</TOCode>
          <VesselAgentCode>…</VesselAgentCode><NoOfContainer>…</NoOfContainer></COARRIHeader>
        <COARRIDetails><COARRIItem>…</COARRIItem>…</COARRIDetails>
      </DocumentDetails>
    </ContLoadingNDischargeOder>

COPRAR (``COPRAR_LOAD_16072026203000.xml``)::

    <AdvContainerList>
      <DocumentHeader>… (same reference block) …</DocumentHeader>
      <DocumentDetails>
        <COPRARHeader><VCN>…</VCN><TOOrDockCode>…</TOOrDockCode><SACode>…</SACode>
          <RotationNumber>…</RotationNumber><RotationNumberDate>04082026</RotationNumberDate></COPRARHeader>
        <COPRARDetailsSummary><COPRARItem>…</COPRARItem>…</COPRARDetailsSummary>
      </DocumentDetails>
    </AdvContainerList>

Timestamps are IST strings ``DDMMYYYY:HH:MM`` (COARRI) / ``DDMMYYYY`` dates
(COPRAR rotation). Parsed to timezone-aware values (+05:30), matching the
CODECO importer's IST stamping.
"""
from __future__ import annotations

import datetime as dt
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from jnpa_shared.iso6346 import is_valid_container_no

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

ROOT_TO_DOC = {"ContLoadingNDischargeOder": "COARRI",
               "AdvContainerList": "COPRAR"}


class EdiVesselParseError(ValueError):
    """Structural failure — unreadable XML / not a COARRI/COPRAR document."""


def _text(el: Optional[ET.Element], tag: str) -> Optional[str]:
    child = el.find(tag) if el is not None else None
    value = (child.text or "").strip() if child is not None else ""
    return value or None


def _num(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _int(value: Optional[str]) -> Optional[int]:
    if value is None or not value.isdigit():
        return None
    return int(value)


def _yn(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    v = value.strip().upper()
    if v in ("Y", "YES", "TRUE", "1"):
        return True
    if v in ("N", "NO", "FALSE", "0", "NONE"):
        return False
    return None


def parse_ist_datetime(value: Optional[str]) -> Optional[dt.datetime]:
    """``DDMMYYYY:HH:MM`` (IST) → aware datetime; None on any mismatch."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d%m%Y:%H:%M").replace(tzinfo=IST)
    except ValueError:
        return None


def parse_ist_date(value: Optional[str]) -> Optional[dt.date]:
    """``DDMMYYYY`` → date; None on mismatch."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value.strip(), "%d%m%Y").date()
    except ValueError:
        return None


def detect_doc_type(xml_text: str) -> Optional[str]:
    """COARRI / COPRAR / None, from the root element (cheap sniff)."""
    for root, doc in ROOT_TO_DOC.items():
        if f"<{root}" in xml_text[:2000]:
            return doc
    return None


def direction_from_filename(filename: str) -> Optional[str]:
    name = (filename or "").upper()
    if "LOAD" in name:
        return "LOAD"
    if "DISCH" in name or "DISCHARGE" in name:
        return "DISCHARGE"
    return None


def _document_reference(root: ET.Element) -> Dict[str, Any]:
    ref = root.find("DocumentHeader/DocumentReference")
    return {"document_number": _text(ref, "DocumentNumber"),
            "common_ref": _text(ref, "CommonRefNumber"),
            "sender_id": _text(ref, "SenderID")}


def parse_document(xml_text: str) -> Tuple[str, Dict[str, Any], List[Dict[str, Any]]]:
    """Parse one COARRI/COPRAR document → (doc_type, header, container rows).

    Raises :class:`EdiVesselParseError` on malformed XML or an unknown root.
    Item-level oddities never abort the file: unknown fields ride in
    ``extra``; a row without a container number is skipped.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise EdiVesselParseError(f"malformed XML: {exc}") from exc
    doc_type = ROOT_TO_DOC.get(root.tag)
    if doc_type is None:
        raise EdiVesselParseError(f"unknown root element <{root.tag}>")
    header = _document_reference(root)
    if doc_type == "COARRI":
        h = root.find("DocumentDetails/COARRIHeader")
        header.update({
            "vcn": _text(h, "VCN"),
            "terminal_code": _text(h, "TOCode"),
            "agent_code": _text(h, "VesselAgentCode"),
            "declared_count": _int(_text(h, "NoOfContainer")),
        })
        items = root.findall("DocumentDetails/COARRIDetails/COARRIItem")
        rows = [r for r in (_coarri_row(i) for i in items) if r is not None]
    else:
        h = root.find("DocumentDetails/COPRARHeader")
        header.update({
            "vcn": _text(h, "VCN"),
            "terminal_code": _text(h, "TOOrDockCode"),
            "agent_code": _text(h, "SACode"),
            "rotation_no": _text(h, "RotationNumber"),
            "rotation_date": parse_ist_date(_text(h, "RotationNumberDate")),
            "declared_count": _int(_text(h, "TotNoContainer")),
        })
        items = root.findall("DocumentDetails/COPRARDetailsSummary/COPRARItem")
        rows = [r for r in (_coprar_row(i) for i in items) if r is not None]
    return doc_type, header, rows


_COARRI_KNOWN = {
    "EquipmentStatusCode", "ContainerNumber", "CustomsContSealNumber",
    "ShipperContainerSealNumber", "CACode", "ContLineCode", "ContISOCode",
    "ICDIndicator", "CShippingDateTime", "CLandDateTime",
    "ContainerDamageIndicator", "ContainerDamageDesc", "BerthingDateTime",
}

_COPRAR_KNOWN = {
    "EquipmentStatusCode", "ContainerNumber", "ContainerStatus",
    "ContainerISOCode", "ContainerTareWeight", "ContainerGrossWeight",
    "PortOfOrigin", "PortOfLoading", "IGMLineNumber", "IGMSubLineNumber",
    "CargoType", "CACode", "IMOClass", "PortOfDischarge",
    "FinalPortOfDischarge",
}


def _extra(item: ET.Element, known: set) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for child in item:
        if child.tag not in known:
            value = (child.text or "").strip()
            if value:
                out[child.tag] = value
    return out


def _coarri_row(item: ET.Element) -> Optional[Dict[str, Any]]:
    container = _text(item, "ContainerNumber")
    if not container:
        return None
    container = container.upper()
    return {
        "container_no": container,
        "iso_valid": is_valid_container_no(container),
        "equipment_status": _text(item, "EquipmentStatusCode"),
        "seal_no": _text(item, "CustomsContSealNumber"),
        "shipper_seal_no": _text(item, "ShipperContainerSealNumber"),
        "line_code": _text(item, "ContLineCode") or _text(item, "CACode"),
        "iso_code": _text(item, "ContISOCode"),
        "icd_indicator": _yn(_text(item, "ICDIndicator")),
        "shipping_ts": parse_ist_datetime(_text(item, "CShippingDateTime")),
        "landing_ts": parse_ist_datetime(_text(item, "CLandDateTime")),
        "berthing_ts": parse_ist_datetime(_text(item, "BerthingDateTime")),
        "damage_indicator": _yn(_text(item, "ContainerDamageIndicator")),
        "damage_desc": _text(item, "ContainerDamageDesc"),
        "extra": _extra(item, _COARRI_KNOWN),
    }


def _coprar_row(item: ET.Element) -> Optional[Dict[str, Any]]:
    container = _text(item, "ContainerNumber")
    if not container:
        return None
    container = container.upper()
    return {
        "container_no": container,
        "iso_valid": is_valid_container_no(container),
        "equipment_status": _text(item, "EquipmentStatusCode"),
        "container_status": _text(item, "ContainerStatus"),
        "iso_code": _text(item, "ContainerISOCode"),
        "tare_weight": _num(_text(item, "ContainerTareWeight")),
        "gross_weight": _num(_text(item, "ContainerGrossWeight")),
        "pol": _text(item, "PortOfLoading") or _text(item, "PortOfOrigin"),
        "pod": _text(item, "PortOfDischarge"),
        "final_pod": _text(item, "FinalPortOfDischarge"),
        "igm_line": _int(_text(item, "IGMLineNumber")),
        "igm_subline": _int(_text(item, "IGMSubLineNumber")),
        "cargo_type": _text(item, "CargoType"),
        "line_code": _text(item, "CACode"),
        "imo_class": _text(item, "IMOClass"),
        "extra": _extra(item, _COPRAR_KNOWN),
    }


__all__ = ["parse_document", "detect_doc_type", "direction_from_filename",
           "parse_ist_datetime", "parse_ist_date", "EdiVesselParseError"]
