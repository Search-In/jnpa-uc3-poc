"""CHPOI13 (SMTP — Sub-Manifest Transhipment Permit) parser.

Shape:  CHPOI13Payload / manifest / trans*  (flat transhipment lines)

Each ``<trans>`` repeats the permit-level fields (SMTPNo, SMTPDate, IGMNo,
DestinationCode, CarrierCode, BondNo) — one permit per file, one destination/bond
per permit (verified across all six customer samples) — plus its own line-level
fields (LineNumber, ContainerNo, SealNo, weight). We lift the permit header from
the first ``<trans>`` and keep the per-container lines separately.
"""
from __future__ import annotations

from typing import Any

from jnpa_shared.iso6346 import is_valid_container_no

from .common import (
    CustomsParseError,
    ParsedMessage,
    clean,
    parse_ddmmyyyy,
    parse_document_header,
    to_int,
    to_num,
)


def _line(el: Any) -> dict[str, Any]:
    cn = clean(el.findtext("ContainerNo"))
    return {
        "line_no": to_int(el.findtext("LineNumber")),
        "subline_no": to_int(el.findtext("SubLineNumber")) or 0,
        "consignee_name": clean(el.findtext("ConsigneeName")),
        "cargo_desc": clean(el.findtext("CargoDesc")),
        "container_no": cn,
        "iso_valid": bool(cn) and is_valid_container_no(cn),
        "container_type": clean(el.findtext("ContainerType")),
        "seal_no": clean(el.findtext("SealNo")),
        "no_of_packages": to_int(el.findtext("NoofPackages")),
        "unit_of_packages": clean(el.findtext("UnitofPackages")),
        "gross_qty": to_num(el.findtext("GrossQtyVolume")),
        "unit_of_qty": clean(el.findtext("UnitofQty")),
    }


def _permit_header(el: Any) -> dict[str, Any]:
    return {
        "customs_house_code": clean(el.findtext("CustomsHouseCode")),
        "smtp_no": clean(el.findtext("SMTPNo")),
        "smtp_date": parse_ddmmyyyy(el.findtext("SMTPDate")),
        "igm_no": clean(el.findtext("IGMNo")),
        "igm_date": parse_ddmmyyyy(el.findtext("IGMDate")),
        "destination_code": clean(el.findtext("DestinationCode")),
        "carrier_code": clean(el.findtext("CarrierCode")),
        "bond_no": clean(el.findtext("BondNo")),
        "terminal_operator_code": clean(el.findtext("TerminalOperatorCode")),
    }


def parse_chpoi13(path: str) -> ParsedMessage:
    """Parse an SMTP (CHPOI13) XML file into a :class:`ParsedMessage`.

    ``payload = {"permits": [ {permit-header..., "lines": [ {line...} ] } ]}``.
    ``record_count`` is the total transhipment line (container) count. A file with
    no ``<trans>`` yields an empty permit list and record_count 0."""
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise CustomsParseError(f"invalid XML in {path}: {exc}") from exc
    root = tree.getroot()
    if root.tag != "CHPOI13Payload":
        raise CustomsParseError(f"not a CHPOI13 payload (root=<{root.tag}>): {path}")

    header = parse_document_header(root.find("DocumentHeader"))
    trans = list(root.iter("trans"))

    permits: list[dict[str, Any]] = []
    if trans:
        permit = _permit_header(trans[0])
        permit["lines"] = [_line(t) for t in trans]
        permits.append(permit)

    message = {
        "message_type": "CHPOI13",
        "module": "SMTP",
        "primary_ref": permits[0]["smtp_no"] if permits else None,
        **header,
    }
    return ParsedMessage(message=message, payload={"permits": permits},
                         record_count=len(trans))
