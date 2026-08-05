"""CALINF (Voyage Registration) → core.vessel_call — the PRE-VCN seed.

The first message in the call lifecycle: it carries IMO + voyage but NO VCN (that
is assigned later by BERMAN). The normalized record therefore has ``vcn=None`` and
is keyed on ``(imo_no, voyage_no)`` — a downstream writer dedupes/promotes on that.
Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import CALL_STATUS_PLANNED, MarineParseError, ft, parse_pcs_dt


def parse_calinf(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    voyage = require(root, "VoyageDetails", "CALINF")
    imo_no = ft(voyage, "IMONumber")
    voyage_no = ft(voyage, "VoyageNumber")
    if not imo_no and not voyage_no:
        raise MarineParseError("CALINF: neither IMONumber nor VoyageNumber present — no call key")

    rec: dict[str, Any] = {
        "_target": "vessel_call",
        "_message": "CALINF",
        "_source_file": source_file,
        "vcn": None,  # pre-VCN: not assigned until BERMAN
        "via_no": None,
        "imo_no": imo_no,
        "vessel_name": ft(voyage, "VesselName"),
        "voyage_no": voyage_no,
        "rotation_no": None,
        "call_sign": ft(voyage, "CallSign"),
        "terminal_code": ft(voyage, "DockORTOCode"),  # resolved to terminal_id downstream
        "purpose": ft(voyage, "PurposeOfvisit"),
        # LIFECYCLE status, not the document's own field. CALINF's <Status> is a PCS
        # record code ('C'/'F' across the corpus) describing the MESSAGE, not the call's
        # operational state, so it is kept separately as `doc_status` and the call is
        # stamped with the stage this message represents.
        "status": CALL_STATUS_PLANNED,
        "doc_status": ft(voyage, "Status"),
        "eta": parse_pcs_dt(ft(voyage, "EDTA")),
        "etd": parse_pcs_dt(ft(voyage, "EDTD")),
        "source_note": ft(root, "CommonRefNumber"),
    }
    return [rec]
