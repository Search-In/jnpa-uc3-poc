"""CALINV (Allotment of VCN) → core.vessel_call — the VCN is issued.

CALINV is the PCS *response* that allots the Vessel Call Number. Its DocumentName is
literally ``ALLOTMENTOFVCN``, and it sits between CALINF and BERMAN once the journals are
read by direction:

    CALINF  (inbound)   voyage registered by the agent, no VCN yet
    CALINV  (outbound)  PCS allots the VCN                 <- this message
    BERMAN  (inbound)   agent's berth application, quotes the allotted VCN
    BERALT  (outbound)  PCS allots the berth

(Doc 05 Chain F places CALINV last as a "close-out". The corpus disagrees — the payload is
an allotment, not a closure — so the ordering above follows the messages, not the doc.)

Corpus shape (546 messages verified across both NLP Outbound reports; root element
``<VesselCallNumber>``, every VCN exactly 14 characters):

    <VoyageDetails>
      <RecordType>N</RecordType>
      <VCN>INNSA1BM0S1088</VCN>       -> call key, and via_no via the tail
      <IMONumber>9136228</IMONumber>
      <CallSign>3EDD</CallSign>
      <VoyageNumber>VN014N</VoyageNumber>   -> POPULATED (unlike BERALT's, always empty)
      <VesselType>5</VesselType>
      <SACode/> <LineCode/>
      <DockORTOCode/>                  -> EMPTY on 535 of 546; deliberately not used
      <Portcode>INJNP1</Portcode>
      <EDTA>23072026:10:24</EDTA>      -> eta
      <EDTD>25072026:08:00</EDTD>      -> etd
      <ServiceName/>                   -> ALWAYS EMPTY in the corpus
      <AllotmentDate>13072026</AllotmentDate>  -> DATE ONLY, see below
    </VoyageDetails>

**Terminal comes from the VCN infix, not DockORTOCode.** The tag is empty on 98% of
messages, and on the one populated message where the two disagree it is the known
NSFT/JNPCT operator-transition ambiguity. The VCN infix resolves 545 of 546. This mirrors
BERMAN, which has no DockORTOCode tag at all.

**No event is emitted.** ``AllotmentDate`` is date-only on all 546 messages (8 characters,
DDMMYYYY) while ``core.vessel_call_event.event_ts`` is ``timestamptz NOT NULL``. Writing it
would stamp every VCN allotment at midnight — a precision the message does not carry. The
value is still parsed as ``allotment_date`` so it is available the moment there is a column
that can hold a date without implying a time.

Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import (CALL_STATUS_VCN_ALLOTTED, MarineParseError, ft, parse_pcs_date,
                         parse_pcs_dt, terminal_from_vcn, via_from_vcn)


def parse_calinv(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """One CALINV document → a single vessel_call record keyed on the allotted VCN."""
    voyage = require(root, "VoyageDetails", "CALINV")
    vcn = ft(voyage, "VCN")
    if not vcn:
        raise MarineParseError("CALINV: VCN (the call key) is missing")

    rec: dict[str, Any] = {
        "_target": "vessel_call",
        "_message": "CALINV",
        "_source_file": source_file,
        "vcn": vcn,
        "via_no": via_from_vcn(vcn),
        "imo_no": ft(voyage, "IMONumber"),
        "vessel_name": None,          # not carried by CALINV
        "voyage_no": ft(voyage, "VoyageNumber"),
        "rotation_no": None,          # assigned later, by BERMAN
        "call_sign": ft(voyage, "CallSign"),
        # VCN infix, NOT DockORTOCode — see the module docstring.
        "terminal_code": terminal_from_vcn(vcn),
        "berth_code": None,           # no berth until BERALT
        "purpose": None,
        "status": CALL_STATUS_VCN_ALLOTTED,
        "eta": parse_pcs_dt(ft(voyage, "EDTA")),
        "etb": None,                  # expected berthing arrives with BERMAN's EDB
        "etd": parse_pcs_dt(ft(voyage, "EDTD")),
        "source_note": ft(root, "CommonRefNumber"),
        # Parsed but NOT bound to a column: date-only, and there is nowhere to store a
        # date without implying a time. See the docstring.
        "allotment_date": parse_pcs_date(ft(voyage, "AllotmentDate")),
    }
    return [rec]
