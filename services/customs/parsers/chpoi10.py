"""CHPOI10 (OOC — Out-Of-Charge / Bill-of-Entry) parser.

OOC files are small (a few KB). Shape:
  CHPOI10Payload / manifest / ooc (bill-of-entry + out-of-charge header)
                                -> ooccont (container)  [1:N]
                                     -> oocitems (invoice item) [1:N]
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


def _item(el: Any) -> dict[str, Any]:
    return {
        "invoice_number": clean(el.findtext("InvoiceNumber")),
        "item_sr_no": to_int(el.findtext("ItemSrNoininvoice")),
        "item_description": clean(el.findtext("Itemdescriptionasdeclared")),
        "hs_classification": clean(el.findtext("HSClassification")),
        "cif_value": to_num(el.findtext("CIFValue")),
        "assessable_value": to_num(el.findtext("Assessablevaluedeclared")),
    }


def _container(el: Any, boe: Any) -> dict[str, Any]:
    cn = clean(el.findtext("ContainerNo"))
    return {
        "container_no": cn,
        "iso_valid": bool(cn) and is_valid_container_no(cn),
        "bill_of_entry_no": boe,
        "items": [_item(it) for it in el.findall("oocitems")],
    }


def _ooc(el: Any) -> dict[str, Any]:
    boe = clean(el.findtext("BillOfEntryNo"))
    return {
        "customs_house_code": clean(el.findtext("CustomsHouseCode")),
        "igm_no": clean(el.findtext("IGMNo")),
        "igm_date": parse_ddmmyyyy(el.findtext("IGMDate")),
        "line_no": to_int(el.findtext("LineNumber")),
        "subline_no": to_int(el.findtext("SubLineNo")) or 0,
        "bill_of_entry_no": boe,
        "bill_of_entry_date": parse_ddmmyyyy(el.findtext("BillOfEntryDate")),
        "document_type": clean(el.findtext("DocumentType")),
        "ie_code": clean(el.findtext("IECode")),
        "importer_name": clean(el.findtext("ImporterName")),
        "importer_address": clean(el.findtext("ImporterAddress")),
        "importer_city": clean(el.findtext("ImporterCity")),
        "pin_code": clean(el.findtext("PINCode")),
        "cha_code": clean(el.findtext("CHACode")),
        "out_of_charge_no": clean(el.findtext("OutOfChargeNo")),
        "out_of_charge_date": parse_ddmmyyyy(el.findtext("OutOfChargeDate")),
        "out_of_charge_type": clean(el.findtext("OutOfChargeType")),
        "nature_of_cargo": clean(el.findtext("NatureOfCargo")),
        "quantity_out_of_charged": to_num(el.findtext("QuantityOutOfCharged")),
        "unit_of_quantity": clean(el.findtext("UnitOfQuantity")),
        "no_of_packages": to_int(el.findtext("NumberOfPackages")),
        "country_of_origin": clean(el.findtext("CountryOfOrigin")),
        "assessable_value": to_num(el.findtext("AssessableValueCustomsassessed")),
        "cif_value": to_num(el.findtext("CIFValueCustomsAssessed")),
        "total_customs_duty": to_num(el.findtext("TotalCustomsDutyPaidAmount")),
        "containers": [_container(c, boe) for c in el.findall("ooccont")],
    }


def parse_chpoi10(path: str) -> ParsedMessage:
    """Parse an OOC (CHPOI10) XML file into a :class:`ParsedMessage`.

    ``payload = {"oocs": [ {ooc..., "containers": [ {container..., "items": [...] } ] } ]}``.
    ``record_count`` is the total OOC container count."""
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise CustomsParseError(f"invalid XML in {path}: {exc}") from exc
    root = tree.getroot()
    if root.tag != "CHPOI10Payload":
        raise CustomsParseError(f"not a CHPOI10 payload (root=<{root.tag}>): {path}")

    header = parse_document_header(root.find("DocumentHeader"))
    oocs = [_ooc(el) for el in root.iter("ooc")]
    total_containers = sum(len(o["containers"]) for o in oocs)

    message = {
        "message_type": "CHPOI10",
        "module": "OOC",
        "primary_ref": oocs[0]["bill_of_entry_no"] if oocs else None,
        **header,
    }
    return ParsedMessage(message=message, payload={"oocs": oocs},
                         record_count=total_containers)
