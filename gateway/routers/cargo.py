"""/api/cargo — CRUD over the shared cargo record (POC-3 as common backend).

POC-3 owns the single ``jnpa.cargo`` table on the shared RDS. This router is the
one CRUD surface over it, consumed by BOTH the POC-3 dashboard and the POC-2
(Cargo Twin) frontend — POC-2 keeps no backend/DB of its own. It is a THIN
router in the same mould as :mod:`gateway.routers.fastag`:

  1. validates the request (Pydantic v2 DTOs below — ISO-6346 for the PK),
  2. delegates ALL persistence to :class:`services.cargo.CargoService`
     (the single orchestration point; raw SQL lives in the repository),
  3. maps the service's typed errors to clean HTTP status codes.

    POST   /api/cargo                      -> 201 Created (409 on duplicate)
    GET    /api/cargo                      -> 200 list (filter + paginate)
    GET    /api/cargo/{container_number}   -> 200 one (404 if absent)
    PUT    /api/cargo/{container_number}   -> 200 updated (404 if absent)
    DELETE /api/cargo/{container_number}   -> 200 deleted (404 if absent)

Invalid payloads (bad ISO-6346, bad enum, bad types) surface as 400 via the
gateway's shared validation handler (see gateway/main.py — /api/cargo/ is mapped
to 400 alongside /api/fastag/).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jnpa_shared.iso6346 import is_valid_container_no

from services.cargo import CargoConflict, CargoNotFound, CargoService

from ..logging import get_logger

log = get_logger("gateway.cargo")

router = APIRouter(prefix="/api/cargo", tags=["cargo"])


# --------------------------------------------------------------------------- deps
# Module-singleton service (built once, DSN from the gateway config), matching the
# FASTag router. Dependency-injected so tests can override with a fake-repo-backed
# service via app.dependency_overrides.
_service: Optional[CargoService] = None


def get_service(request: Request) -> CargoService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = CargoService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


# ------------------------------------------------------------------- validation
def _clean_container_no(value: str) -> str:
    """Normalise + ISO-6346-validate a container number (the follow-the-box PK)."""
    if value is None or not str(value).strip():
        raise ValueError("container_number is required")
    norm = str(value).strip().upper().replace(" ", "")
    if not is_valid_container_no(norm):
        raise ValueError("invalid container_number (ISO-6346 check-digit failed)")
    return norm


class CustomsStatus(str, Enum):
    PENDING = "PENDING"
    CLEARED = "CLEARED"
    HELD = "HELD"
    UNDER_INSPECTION = "UNDER_INSPECTION"


def _clean_vehicle(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    norm = str(value).strip().upper().replace(" ", "")
    return norm or None


# --------------------------------------------------------------------------- DTOs
class CargoCreate(BaseModel):
    container_number: str = Field(..., description="ISO-6346 container number (primary key)")
    vessel_name: Optional[str] = Field(default=None, max_length=200)
    customs_status: CustomsStatus = Field(default=CustomsStatus.PENDING)
    yard_block: Optional[str] = Field(default=None, max_length=50)
    is_released: bool = Field(default=False)
    vehicle_number: Optional[str] = Field(default=None, max_length=32)
    gate: Optional[str] = Field(default=None, max_length=50)
    camera_id: Optional[str] = Field(default=None, max_length=50)
    eta: Optional[datetime] = Field(default=None, description="Estimated time of arrival (ISO-8601)")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "container_number": "MAEU6123458", "vessel_name": "MAERSK SEMBAWANG",
            "customs_status": "PENDING", "yard_block": "A-12", "is_released": False,
            "vehicle_number": "MH04AB1234", "gate": "GATE-3", "camera_id": "CAM-ANPR-03",
            "eta": "2026-07-12T08:30:00Z",
        }
    })

    @field_validator("container_number")
    @classmethod
    def _v_container(cls, v: str) -> str:
        return _clean_container_no(v)

    @field_validator("vehicle_number")
    @classmethod
    def _v_vehicle(cls, v: Optional[str]) -> Optional[str]:
        return _clean_vehicle(v)


class CargoUpdate(BaseModel):
    """All fields optional — only the ones provided are patched. The immutable PK
    (container_number) comes from the path, not the body."""
    vessel_name: Optional[str] = Field(default=None, max_length=200)
    customs_status: Optional[CustomsStatus] = None
    yard_block: Optional[str] = Field(default=None, max_length=50)
    is_released: Optional[bool] = None
    vehicle_number: Optional[str] = Field(default=None, max_length=32)
    gate: Optional[str] = Field(default=None, max_length=50)
    camera_id: Optional[str] = Field(default=None, max_length=50)
    eta: Optional[datetime] = None

    model_config = ConfigDict(json_schema_extra={
        "example": {"customs_status": "CLEARED", "is_released": True, "yard_block": "B-04"}
    })

    @field_validator("vehicle_number")
    @classmethod
    def _v_vehicle(cls, v: Optional[str]) -> Optional[str]:
        return _clean_vehicle(v)


class CargoOut(BaseModel):
    container_number: str
    vessel_name: Optional[str] = None
    customs_status: str
    yard_block: Optional[str] = None
    is_released: bool
    vehicle_number: Optional[str] = None
    gate: Optional[str] = None
    camera_id: Optional[str] = None
    eta: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


_ERROR_RESPONSES = {
    400: {"description": "Validation error (bad ISO-6346 / enum / types)"},
    404: {"description": "Container not found"},
    409: {"description": "Duplicate container_number"},
    500: {"description": "Internal error"},
}


def _to_out(row: dict) -> CargoOut:
    return CargoOut(**row)


def _require_container_no(value: str) -> str:
    """ISO-6346-validate a path container number, mapping failure to 400 (never
    500). Endpoint bodies validate via Pydantic; path params validate here."""
    try:
        return _clean_container_no(value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "validation_error", "detail": str(exc),
                                    "container_number": value})


# --------------------------------------------------------------------- endpoints
@router.post("", response_model=CargoOut, status_code=status.HTTP_201_CREATED,
             responses=_ERROR_RESPONSES, summary="Create a cargo record")
async def create_cargo(
    body: CargoCreate,
    service: CargoService = Depends(get_service),
) -> CargoOut:
    try:
        row = await service.create_cargo(body.model_dump())
    except CargoConflict:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "duplicate_container",
                                    "container_number": body.container_number})
    return _to_out(row)


@router.get("", response_model=list[CargoOut],
            summary="List cargo records (filter + paginate)")
async def list_cargo(
    response: Response,
    container_number: Optional[str] = Query(default=None, description="Exact ISO-6346 match"),
    customs_status: Optional[CustomsStatus] = Query(default=None),
    yard_block: Optional[str] = Query(default=None),
    is_released: Optional[bool] = Query(default=None),
    vehicle_number: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: CargoService = Depends(get_service),
) -> list[CargoOut]:
    # Normalise the identifier filters the same way writes do, so a lookup by a
    # spaced/lower-case value still matches the stored canonical form.
    cn = _require_container_no(container_number) if container_number else None
    veh = _clean_vehicle(vehicle_number)
    filters = dict(
        container_number=cn,
        customs_status=customs_status.value if customs_status else None,
        yard_block=yard_block, is_released=is_released, vehicle_number=veh,
    )
    rows = await service.list_cargo(**filters, limit=limit, offset=offset)
    # Total (pre-pagination) count so a paginated Cargo-Twin UI can render controls.
    response.headers["X-Total-Count"] = str(await service.count_cargo(**filters))
    return [_to_out(r) for r in rows]


@router.get("/{container_number}", response_model=CargoOut, responses=_ERROR_RESPONSES,
            summary="Get one cargo record by container number")
async def get_cargo(
    container_number: str,
    service: CargoService = Depends(get_service),
) -> CargoOut:
    row = await service.get_cargo(_require_container_no(container_number))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found", "container_number": container_number})
    return _to_out(row)


@router.put("/{container_number}", response_model=CargoOut, responses=_ERROR_RESPONSES,
            summary="Update a cargo record")
async def update_cargo(
    container_number: str,
    body: CargoUpdate,
    service: CargoService = Depends(get_service),
) -> CargoOut:
    cn = _require_container_no(container_number)
    try:
        row = await service.update_cargo(cn, body.model_dump(exclude_unset=True))
    except CargoNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found", "container_number": cn})
    return _to_out(row)


@router.delete("/{container_number}", responses=_ERROR_RESPONSES,
               summary="Delete a cargo record")
async def delete_cargo(
    container_number: str,
    service: CargoService = Depends(get_service),
) -> dict:
    cn = _require_container_no(container_number)
    removed = await service.delete_cargo(cn)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found", "container_number": cn})
    return {"deleted": True, "container_number": cn}
