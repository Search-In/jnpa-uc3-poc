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
        "teu_capacity": None,  # not carried by VESPRO
        "mmsi": None,          # VESPRO carries no MMSI
        "engine_type": ft(details, "EngineType"),
        "num_engines": to_int(ft(details, "NoOfEngines")),
        "propulsion_type": ft(details, "PropulsionType"),
        "num_propellers": to_int(ft(details, "Propellers")),
        "max_speed_kn": to_num(ft(details, "MaxManeuveringSpeed")),
        "bow_thruster": yn_bool(ft(details, "BowThruster")),
        "stern_thruster": yn_bool(ft(details, "SternThruster")),
        "built_date": parse_pcs_date(ft(details, "VesselDeliveryDate")),
        "reg_port": ft(details, "RegPort"),
        "owner_name": ft(details, "VesselOwner"),
        "email": ft(details, "VesselEmailID"),
        "vespro_ref": ft(root, "CommonRefNumber"),
        "_insurance": insurance,
    }
    return [rec]
