"""ICEGATE message simulator for the JNPA UC-III PoC.

ICEGATE (the Indian Customs EDI gateway) is the system of record for the
shipping bill, its IGM (Import/Export General Manifest) number and the Let
Export Order (LEO) status that the Auto-LEO process is gating on. The real
integration exchanges EDI/JSON messages; here we synthesise the same fields
deterministically from the seeded dataset so the rest of the system is
API-correct before ICEGATE credentials are provisioned.

The simulator exposes the message as it would arrive on the wire — a flat dict
keyed by shipping bill — plus a couple of lookup helpers used by the FastAPI
surface and the Auto-LEO reconciler.
"""
from __future__ import annotations

from typing import Dict, Optional

from . import seed as seed_mod
from .seed import GateRecord, IcegateRecord


def icegate_message(rec: IcegateRecord) -> dict:
    """Shape an :class:`IcegateRecord` as an ICEGATE-on-the-wire message dict."""
    return {
        "shipping_bill_no": rec.shipping_bill_no,
        "container_no": rec.container_no,
        "leo_status": rec.leo_status,
        "leo_granted": rec.leo_status == "GRANTED",
        "igm_no": rec.igm_no,
        "assessment": rec.assessment,
        "source": "ICEGATE",
    }


def build_sb_index(dataset: Dict[str, GateRecord]) -> Dict[str, IcegateRecord]:
    """Index ICEGATE records by shipping-bill number for SB-keyed lookups."""
    return {rec.icegate.shipping_bill_no: rec.icegate for rec in dataset.values()}


def lookup_by_shipping_bill(
    shipping_bill_no: str, dataset: Optional[Dict[str, GateRecord]] = None
) -> Optional[IcegateRecord]:
    """Return the ICEGATE record for a shipping bill, or ``None`` if unknown."""
    dataset = dataset or seed_mod.generate_dataset()
    return build_sb_index(dataset).get(shipping_bill_no)


def lookup_by_container(
    container_no: str, dataset: Optional[Dict[str, GateRecord]] = None
) -> Optional[IcegateRecord]:
    """Return the ICEGATE record for a container, or ``None`` if unknown."""
    dataset = dataset or seed_mod.generate_dataset()
    rec = dataset.get(container_no)
    return rec.icegate if rec is not None else None
