"""BERMAN (Berth Management) → core.vessel_call — VCN assigned.

BERMAN is where the full PCS VCN appears; it carries IMO + voyage too, so a
downstream writer can PROMOTE the pre-VCN CALINF seed (match on (imo_no, voyage_no),
set vcn) or upsert on vcn directly. Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import MarineParseError, ft, parse_pcs_dt


def parse_berman(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    header = require(root, "BERMANHeader", "BERMAN")
    vcn = ft(header, "VCN")
    if not vcn:
        raise MarineParseError("BERMAN: VCN (the call key) is missing")

    rec: dict[str, Any] = {
        "_target": "vessel_call",
        "_message": "BERMAN",
        "_source_file": source_file,
        "vcn": vcn,
        "via_no": None,
        "imo_no": ft(header, "IMONumber"),
        "vessel_name": ft(header, "VesselName"),
        "voyage_no": ft(header, "VoyageNumber"),
        "rotation_no": ft(header, "RotationNumber"),
        "call_sign": ft(header, "CallSign"),
        "terminal_code": ft(header, "DockORTOCode"),
        "purpose": ft(header, "PurposeOfVisit"),
        "status": None,
        "eta": parse_pcs_dt(ft(header, "EDTA")),
        "etd": parse_pcs_dt(ft(header, "EDTD")),
        "etb": parse_pcs_dt(ft(header, "EDB")),
        "source_note": ft(root, "CommonRefNumber"),
    }
    return [rec]
