"""ACKPLM / PLTMEM (pilot memo) → core.pilotage — the PCS-native pilot movement.

WHY THIS PARSER EXISTS
----------------------
``core.pilotage`` had exactly ONE producer: the pilot-card XLSX, which identifies a
movement by short VIA. The PCS journals carry 102 pilot messages (87 ACKPLM + 15 PLTMEM)
that were being dropped as ``unsupported_message_type`` — and they are keyed by the full
**VCN**, the system's primary correlation key. A call with a PILOT_BOARDED milestone could
therefore have no pilotage row at all, which is exactly the state VCN INNSA1NF0S0776
(TSS AMBER) is in.

    PLTMEM  (inbound)   the agent APPLIES for a pilot          <PilotMemoApplication>
    ACKPLM  (outbound)  PCS ALLOTS the pilot and names them    <PilotMemoAcknowledgment>

Both describe the SAME movement from opposite directions, so both map to one
``_target='pilotage'`` record and the existing insert dedupes them on ``row_sha256``.

NO NEW CORRELATION LOGIC. The record carries ``via_no = via_from_vcn(vcn)``, so the
EXISTING ``_PILOTAGE_INSERT`` VIA lookup resolves ``call_id`` unchanged. No repository SQL,
no API contract and no projection code is touched by this parser.

Corpus shape (102 messages, every tag counted — nothing below is assumed):

    ACKPLM  <PilotMemoAcknowledgmentDetails>          PLTMEM <PilotMemoApplicationDetails>
      VCN                       87/87  non-empty        VCN                      15/15
      IMONumber                 87/87                   IMONumber                15/15
      PilotName                 87/86  <- the pilot     DraftAft / DraftFwd      15/15
      PilotboardingDateTime     87/87  <- boarding      PilotRequiredDateAndTime 15/15
      DraftAft / DraftFwd       87/87                   DateAndTimeOfSubmission  15/15
      DateAndTimeOfSubmission   87/87                   BerthFrom                 3/15
      PlaceOfPilotboarding      87/87  ('Berth')        BerthTo                   2/15
      ApprovalStatus            87/87  ('A')            VesselMovementFrom/To     1/15
      OperationType             87/87  ('O')            OperationType            15/15 ('O')
      Approvalreason            87/0   ALWAYS EMPTY     NextPortCall             15/15

MOVEMENT DIRECTION — derived from OPERATIONAL FACTS, never from an opaque code
-----------------------------------------------------------------------------
``core.pilotage.movement_type`` is ``NOT NULL CHECK IN ('INWARD','OUTWARD','SHIFTING')``,
so a value is required. ``OperationType`` is 'O' on **all 102** messages and is therefore
NOT a direction — it discriminates nothing, and this codebase has already learned that
single-letter PCS codes are record codes, not states (see calinf.py on ``<Status>``).
Mapping 'O' to OUTWARD would be precisely the guess beralt.py refuses to make about
``DockORTOCode``. It is deliberately NOT used.

The direction is taken from where the pilot boards and where the vessel goes:

    VesselMovementTo == 'SEA'                 -> OUTWARD   (PLTMEM says so outright)
    BerthFrom and BerthTo, and they DIFFER    -> SHIFTING  (berth to berth)
    PlaceOfPilotboarding == 'Berth'           -> OUTWARD   (the vessel is ALREADY
                                                            alongside, so it can only be
                                                            leaving — an inbound vessel
                                                            is not at a berth yet)
    otherwise                                 -> INWARD    (boarding at the station,
                                                            fairway or anchorage)

OPEN QUESTION FOR THE CLIENT (does not block ingestion): every corpus message boards at
'Berth', so the whole set resolves OUTWARD. Whether JNPA's PCS also emits inward pilot
memos — and under what ``PlaceOfPilotboarding`` value — is unconfirmed. The rule above
degrades to INWARD for any other place, so an inward memo is handled the day one appears.

PILOT IDENTITY. ACKPLM names the pilot ('KULDEEP RAWAT'); ``core.pilot.pilot_code`` is a
roster code ('JP 72'). The corpus provides no mapping between the two, so ``pilot_code`` is
left None (the insert resolves it or NULLs it) and the NAME is kept verbatim in ``extras``.
Synthesising a code from a name would invent a roster entry and risk colliding with a real
one. Linking the two needs a client-supplied name↔code table.

Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import MarineParseError, ft, parse_pcs_dt, to_num, via_from_vcn

#: Canonical movement vocabulary of core.pilotage (the CHECK constraint).
INWARD, OUTWARD, SHIFTING = "INWARD", "OUTWARD", "SHIFTING"

#: Container element per message type.
_CONTAINER = {
    "ACKPLM": "PilotMemoAcknowledgmentDetails",
    "PLTMEM": "PilotMemoApplicationDetails",
}

#: Fields the dedup hash is taken over — the identity of ONE movement. Deliberately
#: excludes extras and the message type, so the agent's PLTMEM application and PCS's
#: ACKPLM allotment for the same movement collapse to one row IF every canonical value
#: agrees, and stay two rows when the allotment actually adds a boarding time or drafts.
_HASH_KEYS = ("movement_type", "via_no", "imo_no", "pilot_code",
              "draft_fwd_m", "draft_aft_m", "pilot_boarded_at", "submitted_at",
              "from_berth_code", "to_berth_code")


def _row_hash(rec: dict[str, Any]) -> str:
    payload = {k: rec.get(k) for k in _HASH_KEYS}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _movement(details: ET.Element) -> str:
    """Direction from operational facts. See the module docstring for why not OperationType."""
    to_place = (ft(details, "VesselMovementTo") or "").strip().upper()
    if to_place == "SEA":
        return OUTWARD

    berth_from = ft(details, "BerthFrom")
    berth_to = ft(details, "BerthTo")
    if berth_from and berth_to and berth_from.strip().upper() != berth_to.strip().upper():
        return SHIFTING

    if (ft(details, "PlaceOfPilotboarding") or "").strip().upper() == "BERTH":
        return OUTWARD

    return INWARD


def _parse_memo(root: ET.Element, *, message: str,
                source_file: Optional[str] = None) -> list[dict[str, Any]]:
    details = require(root, _CONTAINER[message], message)

    vcn = ft(details, "VCN")
    if not vcn:
        raise MarineParseError(f"{message}: VCN (the call key) is missing")

    # Every non-canonical value is kept verbatim rather than dropped — same posture as the
    # pilot-card parser, which preserves unmapped sheet columns in extras.
    extras: dict[str, Any] = {"vcn": vcn, "_message": message}
    for key, tag in (("pilot_name", "PilotName"),
                     ("place_of_pilot_boarding", "PlaceOfPilotboarding"),
                     ("approval_status", "ApprovalStatus"),
                     ("operation_type", "OperationType"),
                     ("sa_code", "SACode"),
                     ("next_port_call", "NextPortCall"),
                     ("vessel_movement_from", "VesselMovementFrom"),
                     ("vessel_movement_to", "VesselMovementTo"),
                     ("pilot_required_at", "PilotRequiredDateAndTime"),
                     ("readiness_at", "DateAndTimeOfReadiness"),
                     ("rejection_confirmation_at", "RejectionConfirmationDate")):
        value = ft(details, tag)
        if value:
            extras[key] = value
    common_ref = ft(root, "CommonRefNumber")
    if common_ref:
        extras["common_ref"] = common_ref

    rec: dict[str, Any] = {
        "_target": "pilotage",
        "_message": message,
        "_source_file": source_file,
        "movement_type": _movement(details),
        # The correlation key. via_from_vcn returns None for anything that is not a full
        # 14-character VCN, so a malformed VCN yields an UNLINKED row rather than a wrong
        # link — the same resolve-or-NULL posture used for every other FK.
        "via_no": via_from_vcn(vcn),
        "imo_no": ft(details, "IMONumber"),
        "vessel_name": None,          # not carried by either message
        "pilot_code": None,           # roster code unknown; the NAME is in extras
        "vessel_condition": None,     # not carried
        "draft_fwd_m": to_num(ft(details, "DraftFwd")),
        "draft_aft_m": to_num(ft(details, "DraftAft")),
        # ACKPLM alone carries an actual boarding time. PLTMEM's
        # PilotRequiredDateAndTime is a REQUEST, not an actual, so it stays in extras —
        # writing it here would fabricate a milestone the agent only asked for.
        "pilot_boarded_at": parse_pcs_dt(ft(details, "PilotboardingDateTime")),
        "first_line_at": None,
        "all_fast_at": None,
        "pilot_disembarked_at": None,
        "berth_vacated_at": None,
        "anchor_down_at": None,
        "anchor_up_at": None,
        "submitted_at": parse_pcs_dt(ft(details, "DateAndTimeOfSubmission")),
        "from_berth_code": ft(details, "BerthFrom"),
        "to_berth_code": ft(details, "BerthTo"),
        "extras": extras,
    }
    rec["row_sha256"] = _row_hash(rec)
    return [rec]


def parse_ackplm(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """PCS pilot ALLOTMENT — names the pilot and the boarding time."""
    return _parse_memo(root, message="ACKPLM", source_file=source_file)


def parse_pltmem(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    """Agent pilot APPLICATION — drafts, readiness and the berth the vessel moves from."""
    return _parse_memo(root, message="PLTMEM", source_file=source_file)
