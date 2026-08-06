"""VESPRO (Vessel Profile) → core.vessel (+ core.vessel_insurance).

Vessel master particulars: dimensions, engine/propulsion, thrusters, owner, plus
one or more P&I insurance blocks. Emits ONE ``_target='vessel'`` record whose
``_insurance`` list carries the insurance rows (a downstream writer fans them into
core.vessel_insurance). Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .documents import require
from .pcs_common import MarineParseError, ft, parse_pcs_date, to_int, to_num, yn_bool


def _thruster(details: ET.Element, flag_tag: str, count_tag: str) -> Optional[bool]:
    """Thruster fit as a tri-state, read from whichever form the document carries.

    The corpus states the fit two ways and never both: a Y/N flag (``BowThruster``) or a
    count (``TotalNoOfBowThrusters`` / ``TotalNoOfSternThrusters``). There is no
    ``SternThruster`` flag tag at all, so reading only the flag left stern_thruster
    permanently NULL. Flag wins when present; otherwise "count > 0". An ABSENT count is
    not a "no fit" — it stays None, per the yn_bool tri-state rule.
    """
    flag = yn_bool(ft(details, flag_tag))
    if flag is not None:
        return flag
    count = to_int(ft(details, count_tag))
    return None if count is None else count > 0


def parse_vespro(root: ET.Element, *, source_file: Optional[str] = None) -> list[dict[str, Any]]:
    details = require(root, "VesselProfileDetails", "VESPRO")
    imo_no = ft(details, "IMONumber")
    if not imo_no:
        raise MarineParseError("VESPRO: IMONumber (the vessel key) is missing")

    insurance: list[dict[str, Any]] = []
    for ins in root.findall(".//Insurance"):
        club = ft(ins, "PIClubName")
        if club:
            insurance.append({"pi_club": club, "valid_until": parse_pcs_date(ft(ins, "PIInsuranceValidity"))})

    rec: dict[str, Any] = {
        "_target": "vessel",
        "_message": "VESPRO",
        "_source_file": source_file,
        "imo_no": imo_no,
        "vessel_name": ft(details, "VesselName"),
        "call_sign": ft(details, "CallSign"),
        "flag": ft(details, "VesselFlag"),
        "vessel_type": ft(details, "VesselType"),
        "mtmv": ft(details, "MTMV"),
        "loa_m": to_num(ft(details, "LOA")),
        "beam_m": to_num(ft(details, "Beam")),
        "lbp_m": to_num(ft(details, "LBP")),
        "max_draft_m": to_num(ft(details, "MaxDraft")),
        "grt": to_num(ft(details, "GRT")),
        "nrt": to_num(ft(details, "NRT")),
        "dwt": to_num(ft(details, "DWT")),
        # Both ARE carried, but sparsely — TEU in 6 of 9 corpus files, MMSINumber in 2.
        # A sparse tag is still a mapped tag: absent stays None, present is kept.
        "teu_capacity": to_int(ft(details, "TEU")),
        "mmsi": ft(details, "MMSINumber"),
        "engine_type": ft(details, "EngineType"),
        "num_engines": to_int(ft(details, "NoOfEngines")),
        "propulsion_type": ft(details, "PropulsionType"),
        "num_propellers": to_int(ft(details, "Propellers")),
        "max_speed_kn": to_num(ft(details, "MaxManeuveringSpeed")),
        "bow_thruster": _thruster(details, "BowThruster", "TotalNoOfBowThrusters"),
        "stern_thruster": _thruster(details, "SternThruster", "TotalNoOfSternThrusters"),
        "built_date": parse_pcs_date(ft(details, "VesselDeliveryDate")),
        "reg_port": ft(details, "RegPort"),
        "owner_name": ft(details, "VesselOwner"),
        "email": ft(details, "VesselEmailID"),
        "vespro_ref": ft(root, "CommonRefNumber"),
        "_insurance": insurance,
    }
    return [rec]
