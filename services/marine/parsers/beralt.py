"""BERALT (Berth Allotment) → core.vessel_call (berth assigned) + a BERTH_ALLOTTED event.

BERALT is the stage AFTER BERMAN: BERMAN is the berth *application* and carries no berth
field at all, so this is the FIRST message in the lifecycle that names a berth. It also
carries the full VCN, from which the short VIA is recoverable — the only corpus source
for ``core.vessel_call.via_no``.

Corpus shape (364 messages verified across both NLP Outbound reports; every message has
exactly ONE ``<Berthcode>`` despite the plural ``<BerthCodes>`` wrapper):

    <BerthAllotmentDetails>
      <RecordType>N</RecordType>
      <VCN>INNSA1ND0S6544</VCN>          -> call key, and via_no via the tail
      <IMONumber>9680956</IMONumber>
      <CallSign>9V2251</CallSign>
      <VoyageNumber/>                     -> ALWAYS EMPTY in the corpus (364/364)
      <SACode/> <LineCode>2798</LineCode>
      <DockORTOCode/>                     -> deliberately NOT mapped, see below
      <BerthCodes><Berthcode>CCB</Berthcode></BerthCodes>
      <AllotmentDateTime>15072026:20:58</AllotmentDateTime>
      <TentativeorFinal>F</TentativeorFinal>   -> 'F' on 364/364
    </BerthAllotmentDetails>

**Why DockORTOCode is not mapped to a terminal.** It is populated on roughly half the
messages and CONTRADICTS the terminal already established by CALINF/BERMAN: berth CB01
resolves to NSFT by VCN infix (21/21) but to JNPCT by DockORTOCode (14/14), and BM01
carries NSICT and JNPCT dock codes on messages whose VCN says BMCT. Feeding it into
terminal_id would corrupt a value another message established from a consistent source,
so the terminal is left to CALINF/BERMAN and BERALT contributes only the berth.
(NSFT and JNPCT may well be the same quay under an operator transition — `config/
terminals.json` marks that operator "CONFIRM" — but that is a client question, not an
assumption this parser is entitled to make.)

Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import (CALL_STATUS_BERTH_ALLOTTED, MarineParseError, ft, parse_pcs_dt,
                         via_from_vcn)

#: Lifecycle event written to core.vessel_call_event at the allotment timestamp.
EVENT_BERTH_ALLOTTED = "BERTH_ALLOTTED"


def parse_beralt(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """One BERALT document → a vessel_call update record + (when timed) its event."""
    details = require(root, "BerthAllotmentDetails", "BERALT")
    vcn = ft(details, "VCN")
    if not vcn:
        raise MarineParseError("BERALT: VCN (the call key) is missing")

    berth_code = ft(details, "Berthcode")
    allotted_at = parse_pcs_dt(ft(details, "AllotmentDateTime"))

    call: dict[str, Any] = {
        "_target": "vessel_call",
        "_message": "BERALT",
        "_source_file": source_file,
        "vcn": vcn,
        # The corpus VoyageNumber is empty on every message, so the VIA is recovered from
        # the VCN tail instead — verified equal to the journal's VIA_NO column 364/364.
        "via_no": via_from_vcn(vcn),
        "imo_no": ft(details, "IMONumber"),
        "vessel_name": None,          # not carried by BERALT
        "voyage_no": ft(details, "VoyageNumber"),
        "rotation_no": None,
        "call_sign": ft(details, "CallSign"),
        # NOT set from DockORTOCode — see the module docstring.
        "terminal_code": None,
        "berth_code": berth_code,     # resolved to berth_id downstream
        "purpose": None,
        "status": CALL_STATUS_BERTH_ALLOTTED,
        # AllotmentDateTime is when the allotment was ISSUED, not when the vessel is
        # expected alongside, so it must not be written to etb (BERMAN's EDB is the ETB).
        "eta": None,
        "etb": None,
        "etd": None,
        "source_note": ft(root, "CommonRefNumber"),
        # 'F' (final) on every corpus message; kept so a future tentative allotment is
        # distinguishable without another parser change.
        "allotment_kind": ft(details, "TentativeorFinal"),
    }

    records: list[dict[str, Any]] = [call]

    # The event is emitted only when the allotment is actually timed — an untimed
    # milestone would be a fabricated timestamp.
    if allotted_at is not None:
        records.append({
            "_target": "vessel_call_event",
            "_message": "BERALT",
            "_source_file": source_file,
            "vcn": vcn,
            "via_no": via_from_vcn(vcn),
            "imo_no": ft(details, "IMONumber"),
            "rotation_no": None,
            "vessel_name": None,
            "event_type": EVENT_BERTH_ALLOTTED,
            "event_ts": allotted_at,
            "berth_code": berth_code,
            "source_note": ft(root, "CommonRefNumber"),
        })
    return records
