"""/api/corridor-sim — frozen NH-348 20k-truck simulation (UC3-005, read-only).

No real per-truck GPS exists for the demo window, so this traffic is GENERATED.
Every response says so: `simulated: true` and `provenance: "SIMULATED"` are on
the envelope, and the reproducibility triple (seed, seed_version, config_sha256)
is returned so a reader can confirm the run matches the frozen one and was not
reseeded after rehearsal.

    GET /api/corridor-sim/summary  -> run metadata, totals, segment + IN/OUT split
    GET /api/corridor-sim/trucks   -> paginated trucks, filterable by segment/direction

Reads only core.sim_run / core.sim_truck; no measured table is touched.

RBAC: inherits the default "any authenticated role" read-only rule, like /api/dq.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..metrics import REQUESTS
from services.corridor_sim import CorridorSimService
from services.corridor_sim.repository import DEFAULT_RUN_ID

router = APIRouter(prefix="/api/corridor-sim", tags=["corridor-sim"])

_service: Optional[CorridorSimService] = None


def get_service(request: Request) -> CorridorSimService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CorridorSimService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("/summary")
async def summary(run_id: str = Query(DEFAULT_RUN_ID),
                  svc: CorridorSimService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("corridor_sim", "ok").inc()
    out = await svc.summary(run_id)
    if out is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "no_such_run", "run_id": run_id})
    return out


@router.get("/trucks")
async def trucks(run_id: str = Query(DEFAULT_RUN_ID),
                 segment: Optional[str] = Query(None, description="e.g. SEG-04"),
                 direction: Optional[str] = Query(None, description="IN | OUT"),
                 limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0),
                 svc: CorridorSimService = Depends(get_service)) -> Dict[str, Any]:
    if direction and direction not in ("IN", "OUT"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_direction", "allowed": ["IN", "OUT"]})
    REQUESTS.labels("corridor_sim", "ok").inc()
    return await svc.trucks(run_id, segment=segment, direction=direction,
                            limit=limit, offset=offset)
