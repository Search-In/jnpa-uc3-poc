"""Presenter fault-injection control surface.

Lets the demo console force any of the three fallback chains to a specific rung
on demand, so the bid's fallback story becomes a live click:

    POST   /api/control/fault/{domain}   {"rung": "PROVISIONAL"}   -> force
    DELETE /api/control/fault/{domain}                              -> clear one
    DELETE /api/control/fault                                       -> clear all
    GET    /api/control/fault                                       -> current state

``domain`` is one of ``camera`` | ``vahan`` | ``trucks``. The forced rung is read
at the top of each chain's decision function (routers/anpr.py, vahan.py,
trucks.py), short-circuiting the real health cascade. Every change also pushes an
``operator_banner`` WebSocket frame so the dashboard flips the Health Card and
raises the banner immediately.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..fallback import FAULT_DOMAINS, FAULT_RUNGS
from ..logging import get_logger
from ..state import GatewayState, get_state

log = get_logger("gateway.control")

router = APIRouter(prefix="/api/control", tags=["control"])


class FaultBody(BaseModel):
    rung: str


def _validate_domain(domain: str) -> None:
    if domain not in FAULT_DOMAINS:
        raise HTTPException(
            status_code=404,
            detail={"error": "unknown_domain", "domain": domain,
                    "valid": list(FAULT_DOMAINS)},
        )


@router.get("/fault")
async def get_faults(state: GatewayState = Depends(get_state)) -> dict:
    """Current forced rung + severity for every fault domain."""
    snap = state.faults.snapshot()
    return {"domains": snap, "rungs": FAULT_RUNGS}


@router.post("/fault/{domain}")
async def inject_fault(
    domain: str, body: FaultBody, state: GatewayState = Depends(get_state)
) -> dict:
    """Force ``domain`` to ``body.rung`` and raise the Operator Banner."""
    _validate_domain(domain)
    if body.rung not in FAULT_RUNGS[domain]:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_rung", "domain": domain, "rung": body.rung,
                    "valid": list(FAULT_RUNGS[domain])},
        )
    state.faults.force(domain, body.rung)
    log.info("fault_injected", domain=domain, rung=body.rung)
    banner = await state.broadcast_operator_banner()
    return {"forced": {domain: body.rung}, "banner": banner}


@router.delete("/fault/{domain}")
async def clear_fault(domain: str, state: GatewayState = Depends(get_state)) -> dict:
    """Release ``domain`` back to the real health cascade."""
    _validate_domain(domain)
    state.faults.clear(domain)
    log.info("fault_cleared", domain=domain)
    banner = await state.broadcast_operator_banner()
    return {"cleared": domain, "banner": banner}


@router.delete("/fault")
async def clear_all_faults(state: GatewayState = Depends(get_state)) -> dict:
    """Release every domain (reset to baseline)."""
    state.faults.clear_all()
    log.info("faults_cleared_all")
    banner = await state.broadcast_operator_banner()
    return {"cleared": "all", "banner": banner}
