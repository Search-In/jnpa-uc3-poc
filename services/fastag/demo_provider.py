"""Vendor-shaped demo payloads for the three FASTag ULIP APIs.

Used by :mod:`services.fastag.ulip_client` when ``FASTAG_DEMO_MODE=true`` and no
``FASTAG_ULIP_URL`` is configured, so ``/api/fastag/*`` exercises the full
``client -> mapper -> service -> RDS`` pipeline with no external dependency.

This module is a THIN ADAPTER. Every value comes from
:mod:`services.fastag.demo_dataset` — the same module ``scripts/seed_fastag_demo.py``
seeds RDS from — so a live demo fetch and the persisted history agree exactly.
The functions here only reshape that data into the vendor's envelope; the three
signatures are unchanged, and so is every downstream contract.

Shape note: :func:`demo_transactions` returns the batch as an **object** with a
``transactions`` array (what ``FastagTransactionBatch`` models). It previously
returned a bare list under ``data``, which the mapper could only read as an
unmapped ``data`` key — so every demo lookup persisted zero crossings and the
Transactions / Journey / History views rendered empty.
"""
from __future__ import annotations

from typing import Any, Optional

from . import demo_dataset as ds


def demo_balance(rc_number: str) -> dict[str, Any]:
    """RC -> FASTag balance snapshot for one vehicle."""
    return {"status": "success", "data": ds.account_payload(rc_number)}


def demo_transactions(rc_number: str) -> dict[str, Any]:
    """RC -> the vehicle's toll crossings over the trailing 30 days.

    5-10 SUCCESS crossings at real plazas on the corridor that vehicle runs,
    ordered newest first, with the batch-level ``bank_name``/``status`` the
    provider sends once per lookup.
    """
    return {"status": "success", "data": ds.transactions_payload(rc_number)}


def demo_toll_enroute(payload: Optional[dict] = None) -> dict[str, Any]:
    """Toll plazas enroute for the requested route.

    Echoes the request's source/destination/vehicle so the demo reads coherently,
    then answers with the plazas actually on that JNPA corridor (with per-class
    fares). Vendor field names are used so ``map_toll_enroute`` validates this
    exactly like a real ULIP payload.
    """
    p = payload or {}
    data = ds.enroute_payload(
        source_state=p.get("sourceState", ds.SOURCE_STATE),
        source_name=p.get("sourceName", ds.SOURCE_NAME),
        destination_state=p.get("destinationState", "Maharashtra"),
        destination_name=p.get("destinationName", "Pune"),
        vehicle_type=p.get("vehicleType", "TRUCK"),
    )
    data["clientId"] = p.get("clientId")
    return {"status": "success", "data": data}
