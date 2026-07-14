"""/api/driver — the authenticated driver's own profile (Driver PWA).

A signed-in driver views their complete approved profile: driver details, the
assigned vehicle (from the Vehicle Master), and the enrollment/approval record.

Security: the profile is resolved from the caller's OWN identity, never from a
client-supplied id. A DRIVER JWT is device-bound (device_id == the assigned
Vehicle ID); this endpoint reads that device_id from the token and looks up the
ACTIVE driver assigned to it. A DRIVER can therefore only ever see their own
profile — any ``device_id`` query param is IGNORED for a DRIVER principal. The
query param is honoured only when there is no authenticated principal (auth
disabled, local dev) or for a control-room principal (admin support view).

    GET /api/driver/profile -> { driver, vehicle, enrollment }
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import enrollment, fleet
from ..auth import Role
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.driver")

router = APIRouter(prefix="/api/driver", tags=["driver"])


def _resolve_device_id(request: Request, query_device_id: Optional[str]) -> str:
    """Determine WHOSE profile to return, securely.

    - DRIVER principal  -> the token's device_id (authoritative; query ignored).
    - other principal   -> query device_id (control-room support view).
    - no principal      -> query device_id (auth disabled / local dev).
    Raises 401/400 when no identity can be established."""
    principal = getattr(request.state, "principal", None)
    role = getattr(principal, "role", None)
    token_device = getattr(principal, "device_id", None)
    if role == Role.DRIVER.value:
        if not token_device:
            raise HTTPException(status_code=401,
                                detail="driver token is missing its device binding")
        return enrollment.normalize_vehicle_no(token_device)
    candidate = (query_device_id or token_device or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="device_id required")
    return enrollment.normalize_vehicle_no(candidate)


@router.get("/profile")
async def driver_profile(request: Request,
                         device_id: Optional[str] = Query(default=None),
                         state: GatewayState = Depends(get_state)) -> dict:
    """The signed-in driver's own approved profile + assigned vehicle + enrollment."""
    dsn = state.cfg.postgres_dsn
    vehicle_id = _resolve_device_id(request, device_id)

    # Resolve the ACTIVE driver assigned to this vehicle — the same gate the PWA
    # login uses. No active driver -> no viewable profile.
    holder = await enrollment.get_active_driver_by_vehicle(dsn, vehicle_id)
    if not holder:
        raise HTTPException(
            status_code=404,
            detail="No active driver profile is assigned to this vehicle.")
    driver_id = holder.get("driver_id")

    # Full master record (durable identity) + the enrollment/approval record.
    driver = await enrollment.get_driver(dsn, driver_id) or dict(holder)
    enrol = await enrollment.get(dsn, driver_id, include_faces=False) or {}
    vehicle = await fleet.get_vehicle(dsn, vehicle_id)

    approved_at = driver.get("enrolled_at") or enrol.get("reviewed_at")
    approved_by = driver.get("approved_by") or enrol.get("reviewed_by")

    REQUESTS.labels("driver", "ok").inc()
    return {
        "driver": {
            "id": driver.get("driver_id"),
            "name": driver.get("name"),
            "mobile": driver.get("mobile"),
            "licence": driver.get("license_no"),
            "emergency_contact": driver.get("emergency_contact"),
            "status": driver.get("status"),
        },
        "vehicle": {
            "vehicle_id": vehicle_id,
            "vehicle_number": (vehicle or {}).get("vehicle_number")
            or driver.get("vehicle_no"),
            "vehicle_type": (vehicle or {}).get("vehicle_type"),
            "chassis_number": (vehicle or {}).get("chassis_number"),
            "rfid_fastag_id": (vehicle or {}).get("rfid_fastag_id"),
            "status": (vehicle or {}).get("status"),
        },
        "enrollment": {
            "status": enrol.get("status") or driver.get("status"),
            "approved_at": approved_at,
            "approved_by": approved_by,
        },
    }
