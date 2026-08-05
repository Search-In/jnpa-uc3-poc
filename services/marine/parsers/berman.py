"""BERMAN (Berth Management) → core.vessel_call — VCN assigned.

BERMAN is where the full PCS VCN appears; it carries IMO + voyage too, so a
downstream writer can PROMOTE the pre-VCN CALINF seed (match on (imo_no, voyage_no),
set vcn) or upsert on vcn directly. Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import (CALL_STATUS_BERTH_PLANNED, MarineParseError, ft, parse_pcs_dt,
                         terminal_from_vcn)


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
        # BERMAN has NO DockORTOCode tag — the terminal is encoded only in the VCN infix
        # (INNSA1**BM**0R3119 -> BMCT). Falls back to the tag in case a variant carries it.
        "terminal_code": ft(header, "DockORTOCode") or terminal_from_vcn(vcn),
        # 'PurposeOfvisit' in the corpus; the capitalised spelling is kept as a fallback
        # because misspelled tags ARE the PCS schema (see BERMAN 'DestinationPortl').
        "purpose": ft(header, "PurposeOfVisit") or ft(header, "PurposeOfvisit"),
        # Lifecycle stage this message represents — the VCN is now allotted.
        "status": CALL_STATUS_BERTH_PLANNED,
        "eta": parse_pcs_dt(ft(header, "EDTA")),
        "etd": parse_pcs_dt(ft(header, "EDTD")),
        "etb": parse_pcs_dt(ft(header, "EDB")),
        "source_note": ft(root, "CommonRefNumber"),
    }
    return [rec]
