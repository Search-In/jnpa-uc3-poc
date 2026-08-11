"""/api/vehicle-registry — vehicle -> transporter mappings (UC3-004, read-only).

Gap G6: the customer's transporter and PDP masters carry no vehicle numbers, so
only the REAL gate-document corpus can evidence a mapping. This router serves the
registry seeded by scripts/seed_uc3_004_vehicle_registry.py and keeps the two
halves apart in the payload itself:

    DOCUMENT_EVIDENCED -> source_ref names the gate document it was read from
    SYNTHETIC          -> assumption_ref 'A-G6' + the seed that generated it

    GET /api/vehicle-registry/mappings         -> list / filter / paginate
    GET /api/vehicle-registry/vehicle/{plate}  -> one mapping, 404 when unknown
    GET /api/vehicle-registry/summary          -> provenance roll-up + assumption

Nothing here writes, and nothing infers a transporter: a plate with no row 404s
rather than guessing.

RBAC: /api/vehicle-registry is not in gateway/auth.py._POLICY, so it inherits the
default "any authenticated role" rule (read-only), exactly like /api/dq.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..metrics import REQUESTS
from services.vehicle_registry import VehicleRegistryService
from services.vehicle_registry.repository import VALID_PROVENANCE

router = APIRouter(prefix="/api/vehicle-registry", tags=["vehicle-registry"])

_service: Optional[VehicleRegistryService] = None


def get_service(request: Request) -> VehicleRegistryService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = VehicleRegistryService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("/mappings")
async def list_mappings(
    provenance: Optional[str] = Query(None, description="DOCUMENT_EVIDENCED | SYNTHETIC"),
    q: Optional[str] = Query(None, description="plate or transporter substring"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    svc: VehicleRegistryService = Depends(get_service),
) -> Dict[str, Any]:
    if provenance and provenance not in VALID_PROVENANCE:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "invalid_provenance",
                                    "allowed": list(VALID_PROVENANCE)})
    REQUESTS.labels("vehicle_registry", "ok").inc()
    return await svc.list_mappings(provenance=provenance, q=q, limit=limit, offset=offset)


@router.get("/vehicle/{plate}")
async def by_vehicle(plate: str,
                     svc: VehicleRegistryService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("vehicle_registry", "ok").inc()
    row = await svc.by_vehicle(plate)
    if row is None:
        # No mapping is a legitimate answer. Inventing a transporter is not.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "no_mapping", "vehicle_no": plate})
    return row


@router.get("/summary")
async def summary(svc: VehicleRegistryService = Depends(get_service)) -> Dict[str, Any]:
    REQUESTS.labels("vehicle_registry", "ok").inc()
    return await svc.summary()
