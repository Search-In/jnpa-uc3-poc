"""Parser for the Electronic Delivery Order (EDO) feed.

The EDO is delivered as an .xlsx whose ``CODOCO`` sheet has two columns
(``MESSAGE NAME`` = ``CODECO``, ``PAYLOAD`` = a full ``<CODECODetails>`` XML
document). Each document carries a DocumentHeader, a CODECOHeader (vessel /
shipping-agent), and one or more Container blocks — we emit one canonical delivery
order per container.
"""
from __future__ import annotations

from typing import Any, Optional
from xml.etree import ElementTree as ET

from .common import (
    ParsedList,
    ShippingLineParseError,
    clean,
    container_valid,
    norm_container,
    parse_ddmmyyyy,
    parse_ddmmyyyy_time,
    parse_ddmmyyyyhhmmss,
)
from .flat_tabular import _read_xlsx


def _t(el: Optional[ET.Element], tag: str) -> Optional[str]:
    if el is None:
        return None
    return clean(el.findtext(tag))


def _parse_document(xml: str) -> list[dict[str, Any]]:
    """Parse one <CODECODetails> document into one dict per container."""
    root = ET.fromstring(xml)
    header = root.find("DocumentHeader/DocumentReference")
    exchange = root.find("DocumentHeader/DocumentExchangeDetails/ReceivingPartyDetails")
    cargo_hdr = root.find("DocumentDetails/CODECOHeader")
    summary = root.find("DocumentSummary")

    base = {
        "document_number": _t(header, "DocumentNumber"),
        "common_ref_number": _t(header, "CommonRefNumber"),
        "message_type": _t(header, "MessageType"),
        "sender_id": _t(header, "SenderID"),
        "receiving_party": _t(exchange, "ReceivingParty"),
        "vcn": _t(cargo_hdr, "VCN"),
        "imo_number": _t(cargo_hdr, "IMONumber"),
        "call_sign": _t(cargo_hdr, "CallSign"),
        "stuff_destuff_flag": _t(cargo_hdr, "StuffDestuffFlag"),
        "shipping_agent_code": _t(cargo_hdr, "ShippingAgentCode"),
        "vessel_country": _t(cargo_hdr, "VesselCountry"),
        "total_containers": _to_int(_t(cargo_hdr, "TotNoContainer")),
        "issued_ts": parse_ddmmyyyyhhmmss(_t(summary, "IssuedDateTime")),
        "raw_xml": xml,
    }

    out: list[dict[str, Any]] = []
    for cont in root.findall("DocumentDetails/ContainerDetails/Container"):
        container_no = norm_container(_t(cont, "ContainerNO"))
        if not container_no:
            continue
        out.append({
            **base,
            "container_no": container_no,
            "container_valid_iso": container_valid(container_no),
            "iso_code": _t(cont, "ContISOCode"),
            "equipment_status": _t(cont, "EquipmentStatusCode"),
            "cargo_type": _t(cont, "CargoType"),
            "loading_port": _t(cont, "LoadingPort"),
            "dest_port": _t(cont, "DestPort"),
            "final_pod": _t(cont, "FinalPortOfDischarge"),
            "arrival_ts": parse_ddmmyyyy_time(_t(cont, "ArrivalDateTime")),
            "receipt_date": parse_ddmmyyyy(_t(cont, "ReceiptDate")),
            "delivery_mode": _t(cont, "DeliveryMode"),
            "gate_pass_no": _t(cont, "GatePassNo"),
            "gate_pass_ts": parse_ddmmyyyy_time(_t(cont, "GatePassDateTime")),
            "vehicle_no": _t(cont, "VehicleNo"),
            "gate_number": _t(cont, "GateNumber"),
            "ca_code": _t(cont, "CACode"),
            "con_seal_status": _t(cont, "ConSealStatus"),
        })
    return out


def _to_int(value: Optional[str]) -> Optional[int]:
    s = clean(value)
    if not s or not s.isdigit():
        return None
    return int(s)


def parse_edo(path: str, *, terminal: str = "OTHER") -> ParsedList:
    """Parse the EDO workbook into canonical delivery-order rows (one per container)."""
    try:
        rows = _read_xlsx(path)
    except Exception as exc:  # noqa: BLE001 — unreadable workbook is structural
        raise ShippingLineParseError(f"cannot read EDO workbook {path}: {exc}") from exc
    if not rows:
        raise ShippingLineParseError(f"empty EDO workbook: {path}")

    header = [clean(c) or "" for c in rows[0]]
    try:
        payload_idx = [h.upper() for h in header].index("PAYLOAD")
    except ValueError:
        payload_idx = 1  # sheet layout is MESSAGE NAME | PAYLOAD

    orders: list[dict[str, Any]] = []
    for raw in rows[1:]:
        if payload_idx >= len(raw):
            continue
        xml = clean(raw[payload_idx])
        if not xml or "<CODECODetails" not in xml:
            continue
        try:
            orders.extend(_parse_document(xml))
        except ET.ParseError:
            # A single malformed payload must not abort the whole file.
            continue

    return ParsedList(
        header={"list_type": "EDO", "terminal": terminal, "vessel_visit": None,
                "voyage": None, "line_code": None, "direction": None},
        delivery_orders=orders,
        record_count=len(orders),
    )
