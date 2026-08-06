"""PAISPS (Pre-Arrival Notification ISPS) → core.vessel_call_event.

The LIVE dt.jnpa.in corpus delivers PAISPS (``<Pre-ArrivalNotificationISPS>``)
in the nlp-marine group — 104 documents on the first backfill, all REJECTED as
"unsupported PCS message type" because the sample pack never contained one.

The declaration is a security milestone on the call: one
``_target='vessel_call_event'`` record of type ``ISPS_DECLARED``, stamped with
the document's IssuedDateTime (fallback: the declared EDTA). Call-resolution
keys mirror vesarr_vesdep: VCN is present on every live document; IMO/voyage
ride along when populated. Pure — returns records, never touches the DB.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any, Optional

from .pcs_common import ft, parse_pcs_dt


def parse_paisps(root: ET.Element, *,
                 source_file: Optional[str] = None) -> list[dict[str, Any]]:
    ts = (parse_pcs_dt(ft(root, "IssuedDateTime"))
          or parse_pcs_dt(ft(root, "EDTA")))
    if ts is None:
        return []  # no timestamp to anchor the milestone — typed warn upstream
    return [{
        "_target": "vessel_call_event",
        "_message": "PAISPS",
        "_source_file": source_file,
        # Call-resolution keys — resolved to call_id downstream (not here).
        "vcn": ft(root, "VCN"),
        "via_no": ft(root, "VoyageNumber"),
        "imo_no": ft(root, "IMONumber"),
        "rotation_no": None,
        "vessel_name": ft(root, "VesselName"),
        "event_type": "ISPS_DECLARED",
        "event_ts": ts,
        "berth_code": None,
        # Compact security context in the free-text note (the event table has
        # no dedicated columns for it): level, ISSC, DG flag, current port.
        "source_note": "; ".join(
            f"{label}={value}" for label, value in (
                ("ref", ft(root, "CommonRefNumber")),
                ("security_level", ft(root, "PresentLevelOfSecurityOnBoard")),
                ("issc", ft(root, "InternationalShipSecurityCertificate")),
                ("dg_on_board", ft(root, "DangerousCargoOnBoard")),
                ("current_port", ft(root, "CurrentPort")),
            ) if value),
    }]
