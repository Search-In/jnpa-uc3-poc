"""/api/vehicles — Vehicle Master (fleet registry) administration.

The authoritative registry of vehicles that a driver may be assigned to. Every
vehicle exists here first (ACTIVE / INACTIVE / MAINTENANCE); the Control-Room
"assign vehicle" dropdown draws ONLY from ``GET /api/vehicles/available`` (ACTIVE
vehicles not yet held by an active driver / open enrollment). The truck-sim fleet
is migrated in on boot so no existing vehicle disappears.

    GET  /api/vehicles              -> master list (search `q`, filter `status`)
    GET  /api/vehicles/stats        -> total / active / assigned / available
    GET  /api/vehicles/available    -> assignable vehicles (dropdown source)
    POST /api/vehicles              -> register a vehicle
    PATCH/api/vehicles/{vehicle_id} -> edit fields / change status
    POST /api/vehicles/sync-fleet   -> re-migrate truck-sim devices (idempotent)

RBAC: mirrors /api/identity (CUSTOMS + DTCCC_ADMIN) — see gateway/auth.py _POLICY.
"""
from __future__ import annotations

from typing import List, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .. import enrollment, fleet
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.vehicles")

router = APIRouter(prefix="/api/vehicles", tags=["vehicles"])


def _actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"{principal.role}:{principal.sub}"
    return request.client.host if request.client else "anonymous"


class CreateVehicleBody(BaseModel):
    # vehicle_id is NOT accepted: the backend owns the TRK sequence. vehicle_number
    # (the human plate) is the required, dedup'd identifier.
    vehicle_number: str
    vehicle_type: Optional[str] = None
    chassis_number: Optional[str] = None
    rfid_fastag_id: Optional[str] = None
    status: Optional[str] = None


class UpdateVehicleBody(BaseModel):
    vehicle_number: Optional[str] = None
    vehicle_type: Optional[str] = None
    chassis_number: Optional[str] = None
    rfid_fastag_id: Optional[str] = None
    status: Optional[str] = None


async def _fleet_devices(state: GatewayState, limit: int) -> List[dict]:
    """Truck-sim device snapshots (best-effort — empty list if the sim is down)."""
    url = state.cfg.truck_api_url.rstrip("/") + "/devices/list"
    try:
        resp = await state.http.get(url, params={"limit": str(limit)})
    except httpx.HTTPError as exc:
        log.warning("fleet_sync_sim_unreachable", error=str(exc))
        return []
    if resp.status_code == 200:
        return list(resp.json().get("devices", []))
    return []


async def _ensure_seeded(state: GatewayState) -> None:
    """Lazily migrate the truck-sim fleet into the master the first time the master
    is read and found empty. Belt-and-braces with the boot-time seed so the demo
    always shows vehicles even if the sim was down at startup."""
    existing = await fleet.list_vehicles(state.cfg.postgres_dsn, limit=1)
    if existing:
        return
    devices = await _fleet_devices(state, 2000)
    if devices:
        await fleet.sync_from_fleet(state.cfg.postgres_dsn, devices)


@router.get("")
@router.get("/")
async def list_vehicles(request: Request,
                        q: Optional[str] = Query(default=None),
                        status: Optional[str] = Query(default=None),
                        limit: int = Query(default=500, ge=1, le=5000),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Vehicle master list with the assigned active driver joined per row."""
    await _ensure_seeded(state)
    dsn = state.cfg.postgres_dsn
    rows = await fleet.list_vehicles(dsn, q=q, status=status, limit=limit)
    drivers = await enrollment.active_driver_vehicle_map(dsn)
    for r in rows:
        holder = drivers.get(enrollment.normalize_vehicle_no(r.get("vehicle_id")))
        r["assigned_driver"] = holder  # {driver_id, name} | None
    REQUESTS.labels("vehicles", "ok").inc()
    return {"vehicles": rows, "count": len(rows)}


@router.get("/stats")
async def vehicle_stats(request: Request,
                        state: GatewayState = Depends(get_state)) -> dict:
    await _ensure_seeded(state)
    dsn = state.cfg.postgres_dsn
    assigned = await enrollment.assigned_vehicles(dsn)
    return await fleet.stats(dsn, assigned)


@router.get("/available")
async def available_vehicles(request: Request,
                             q: Optional[str] = Query(default=None),
                             limit: int = Query(default=50, ge=1, le=500),
                             state: GatewayState = Depends(get_state)) -> dict:
    """ACTIVE master vehicles not already assigned to an active driver / open
    enrollment — the Control-Room 'assign vehicle' dropdown source."""
    await _ensure_seeded(state)
    dsn = state.cfg.postgres_dsn
    assigned = await enrollment.assigned_vehicles(dsn)
    vehicles = await fleet.list_available(dsn, assigned, q=q, limit=limit)
    REQUESTS.labels("vehicles", "ok").inc()
    return {"vehicles": vehicles, "count": len(vehicles)}


@router.post("")
@router.post("/")
async def create_vehicle(request: Request, body: CreateVehicleBody,
                         state: GatewayState = Depends(get_state)) -> dict:
    """Register a new vehicle in the master (admin-only via RBAC).

    The Vehicle ID is generated by the backend (next in the TRK sequence) — the
    admin never types it. Duplicate detection is on ``vehicle_number`` (the plate)."""
    dsn = state.cfg.postgres_dsn
    await _ensure_seeded(state)  # so the sequence starts above the migrated fleet
    vehicle_number = (body.vehicle_number or "").strip()
    if not vehicle_number:
        raise HTTPException(status_code=400, detail="vehicle_number is required")
    status = (body.status or fleet.ACTIVE).strip().upper()
    if status not in fleet.STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {', '.join(fleet.STATUSES)}")
    # Duplicate guard on the human plate, not the machine id.
    dup = await fleet.find_by_number(dsn, vehicle_number)
    if dup:
        raise HTTPException(status_code=409, detail={
            "error": "vehicle_number_exists", "vehicle_number": vehicle_number,
            "vehicle_id": dup.get("vehicle_id"),
            "message": f"Vehicle number {vehicle_number} is already registered as "
                       f"{dup.get('vehicle_id')}."})
    # Generate the Vehicle ID and insert; retry on the (rare) unique-id race.
    rec = None
    for _ in range(5):
        vehicle_id = await fleet.next_vehicle_id(dsn)
        try:
            rec = await fleet.add_vehicle(
                dsn, vehicle_id=vehicle_id, vehicle_number=vehicle_number,
                vehicle_type=(body.vehicle_type or "").strip(),
                chassis_number=(body.chassis_number or "").strip(),
                rfid_fastag_id=(body.rfid_fastag_id or "").strip(),
                status=status, created_by=_actor(request))
            break
        except ValueError:
            continue  # id was taken between generate + insert — regenerate
    if rec is None:
        raise HTTPException(status_code=503,
                            detail="could not allocate a Vehicle ID; try again")
    REQUESTS.labels("vehicles", "ok").inc()
    return {"created": True, "vehicle": rec}


@router.patch("/{vehicle_id}")
async def update_vehicle(vehicle_id: str, request: Request,
                         body: UpdateVehicleBody,
                         state: GatewayState = Depends(get_state)) -> dict:
    """Edit a vehicle's fields or change its status (ACTIVE/INACTIVE/MAINTENANCE).

    Deactivating a vehicle that an active driver still holds is refused (409) so a
    live PWA login can never point at an INACTIVE/MAINTENANCE vehicle."""
    dsn = state.cfg.postgres_dsn
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if "status" in fields:
        st = str(fields["status"]).strip().upper()
        if st not in fleet.STATUSES:
            raise HTTPException(status_code=400,
                                detail=f"status must be one of {', '.join(fleet.STATUSES)}")
        fields["status"] = st
        if st != fleet.ACTIVE:
            holder = await enrollment.get_active_driver_by_vehicle(dsn, vehicle_id)
            if holder:
                raise HTTPException(status_code=409, detail={
                    "error": "vehicle_assigned",
                    "message": f"Vehicle is assigned to active driver "
                               f"{holder.get('name') or holder.get('driver_id')}; "
                               f"reassign before setting {st}."})
    for k in ("vehicle_number", "chassis_number", "rfid_fastag_id", "vehicle_type"):
        if k in fields:
            fields[k] = str(fields[k]).strip() or None
    rec = await fleet.update_vehicle(dsn, vehicle_id, fields=fields)
    if rec is None:
        raise HTTPException(status_code=404, detail=f"vehicle {vehicle_id} not found")
    REQUESTS.labels("vehicles", "ok").inc()
    return {"updated": True, "vehicle": rec}


@router.post("/sync-fleet")
async def sync_fleet(request: Request,
                     state: GatewayState = Depends(get_state)) -> dict:
    """Re-migrate truck-sim devices into the master (idempotent — never clobbers an
    operator edit). Returns how many new vehicles were added."""
    devices = await _fleet_devices(state, 5000)
    inserted = await fleet.sync_from_fleet(state.cfg.postgres_dsn, devices)
    REQUESTS.labels("vehicles", "ok").inc()
    return {"synced": True, "inserted": inserted, "devices_seen": len(devices)}
