"""/api/debug — demo-evidence introspection.

    GET /api/debug/decisions          -> the last N DecisionPath records,
                                         newest first (ring buffer; spec: 1000).
    GET /api/debug/decisions?api=vahan -> filter to one API.

This is the "show which fallback path was used per request" evidence the demo
relies on. The ring buffer lives in-process (gateway.fallback.DecisionRing) and
is capped at GATEWAY_DECISION_RING_SIZE (default 1000).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from ..state import GatewayState, get_state

router = APIRouter(prefix="/api/debug", tags=["debug"])


@router.get("/decisions")
async def decisions(
    api: Optional[str] = Query(default=None, description="filter to one api (vahan/anpr/traffic/trucks)"),
    limit: int = Query(default=1000, ge=1, le=1000),
    state: GatewayState = Depends(get_state),
) -> JSONResponse:
    """Return the ring buffer newest-first as a bare JSON array.

    A bare array (not an object) so the verification command
    ``curl .../api/debug/decisions | jq '.[0]'`` indexes the latest decision.
    """
    items = state.decisions.recent()
    if api:
        items = [d for d in items if d.api == api]
    items = items[:limit]
    return JSONResponse(content=[d.model_dump(mode="json") for d in items])


@router.get("/decisions/summary")
async def decisions_summary(state: GatewayState = Depends(get_state)) -> dict:
    """Counts per (api, decision_path) over the current ring — quick demo view."""
    counts: dict = {}
    for d in state.decisions.recent():
        counts.setdefault(d.api, {}).setdefault(d.decision_path, 0)
        counts[d.api][d.decision_path] += 1
    return {"buffered": len(state.decisions), "counts": counts}
