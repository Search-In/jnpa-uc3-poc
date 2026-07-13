"""/api/cargo — CRUD over the shared cargo record (POC-3 as common backend).

POC-3 owns the single ``jnpa.cargo`` table on the shared RDS. This router is the
one CRUD surface over it, consumed by BOTH the POC-3 dashboard and the POC-2
(Cargo Twin) frontend — POC-2 keeps no backend/DB of its own. It is a THIN
router in the same mould as :mod:`gateway.routers.fastag`:

  1. validates the request (Pydantic v2 DTOs below — ISO-6346 for the PK),
  2. delegates ALL persistence to :class:`services.cargo.CargoService`
     (the single orchestration point; raw SQL lives in the repository),
  3. maps the service's typed errors to clean HTTP status codes.

    POST   /api/cargo                                  -> 201 Created (409 on duplicate)
    GET    /api/cargo                                  -> 200 list (filter + paginate + role scope)
    GET    /api/cargo/events                           -> 200 lifecycle events (notifications)
    GET    /api/cargo/{container_number}               -> 200 one (404 if absent)
    PUT    /api/cargo/{container_number}               -> 200 updated (404 if absent)
    PUT    /api/cargo/{container_number}/yard-assignment -> 200 assigned (404/400)
    DELETE /api/cargo/{container_number}               -> 200 deleted (404 if absent)

  POC-2 extension surface (all ADDITIVE; migrations 0016-0018):

    POST   /api/cargo/notifications                    -> 201 stakeholder notification
    GET    /api/cargo/notifications                    -> 200 list (container/type/severity/status)
    POST   /api/cargo/{container_number}/workflow      -> 200 TRIGGER/APPROVE/REJECT (404 if absent)
    GET    /api/cargo/{container_number}/workflow/history -> 200 append-only transitions
    POST   /api/cargo/yard-planning                    -> 201 plan a yard slot
    GET    /api/cargo/yard-optimization                -> 200 congestion + move recommendations
    POST   /api/cargo/rake-planning                    -> 201 plan a rail rake
    GET    /api/cargo/rake-planning                    -> 200 list rake plans
    POST   /api/cargo/reefer-planning                  -> 201 allocate a reefer slot

  IMPORTANT (route ordering): the static-path GET routes above (/notifications,
  /yard-optimization, /rake-planning) are declared BEFORE GET /{container_number},
  which would otherwise capture them as a container number — same as /events.

Migration 0015 adds four backward-compatible fields (eseal_status, eseal_number,
pre_document_status, origin_stream) and a cargo lifecycle event log that backs the
UC-2 notifications contract (GET /api/cargo/events). GET /api/cargo also accepts an
optional ``role`` scope (an authenticated principal's role wins over the param).

The yard-assignment endpoint is a narrow, single-purpose write over the same
``jnpa.cargo.yard_block`` column that PUT already patches — it exists so POC-2
(Cargo Twin) has one intent-revealing call for "put this box in a block" that
returns a compact {container_number, yard_block, status} envelope. It reuses the
CargoService/CargoRepository update path; no separate yard table or service.

Invalid payloads (bad ISO-6346, bad enum, bad types) surface as 400 via the
gateway's shared validation handler (see gateway/main.py — /api/cargo/ is mapped
to 400 alongside /api/fastag/).
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from jnpa_shared.iso6346 import is_valid_container_no

from services.cargo import (
    CargoConflict,
    CargoNotFound,
    CargoService,
    scope_filters_for_role,
)

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


class ESealStatus(str, Enum):
    """Electronic-seal state for a container (e-Seal contract). ``NONE`` means the
    box carries no e-Seal; the column is also nullable for "unknown / not set"."""
    ACTIVE = "ACTIVE"
    ARMED = "ARMED"
    TAMPERED = "TAMPERED"
    REMOVED = "REMOVED"
    NONE = "NONE"


class PreDocumentStatus(str, Enum):
    """State of the pre-gate document (pre-document) workflow for a container."""
    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


def _clean_vehicle(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    norm = str(value).strip().upper().replace(" ", "")
    return norm or None


def _clean_text(value: Optional[str]) -> Optional[str]:
    """Trim a free-text field; empty/whitespace collapses to None (so an empty
    origin_stream / eseal_number is stored as NULL, not '')."""
    if value is None:
        return None
    norm = str(value).strip()
    return norm or None


# Yard block identifier: a letter/letter-pair, a hyphen, then a 1-3 digit slot
# (e.g. A-01, B-9, AB-123). A required, format-checked value — so an empty or
# malformed block is rejected as a validation error (-> 400) rather than silently
# persisted. Normalised to upper-case with internal spaces stripped.
_YARD_BLOCK_RE = re.compile(r"^[A-Z]{1,2}-\d{1,3}$")


def _clean_yard_block(value: str) -> str:
    """Normalise + validate a yard block. Raises ValueError (-> 400) if empty or
    not matching the ``<LETTERS>-<DIGITS>`` shape."""
    if value is None or not str(value).strip():
        raise ValueError("yard_block is required")
    norm = str(value).strip().upper().replace(" ", "")
    if not _YARD_BLOCK_RE.match(norm):
        raise ValueError("invalid yard_block (expected e.g. 'A-01')")
    return norm


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
    # ---- Contract extensions (migration 0015). All optional -> backward compatible.
    eseal_status: Optional[ESealStatus] = Field(default=None, description="Electronic-seal state")
    eseal_number: Optional[str] = Field(default=None, max_length=64, description="e-Seal id / number")
    pre_document_status: Optional[PreDocumentStatus] = Field(default=None, description="Pre-document workflow state")
    # Accepts either ``origin_stream`` or the camelCase ``originStream`` on input;
    # always serialised back as ``origin_stream``.
    origin_stream: Optional[str] = Field(default=None, max_length=50, alias="originStream",
                                         description="Cargo source stream, e.g. 'UC-II'")

    model_config = ConfigDict(populate_by_name=True, json_schema_extra={
        "example": {
            "container_number": "MAEU6123458", "vessel_name": "MAERSK SEMBAWANG",
            "customs_status": "PENDING", "yard_block": "A-12", "is_released": False,
            "vehicle_number": "MH04AB1234", "gate": "GATE-3", "camera_id": "CAM-ANPR-03",
            "eta": "2026-07-12T08:30:00Z", "eseal_status": "ACTIVE",
            "eseal_number": "ES-88213", "pre_document_status": "COMPLETED",
            "origin_stream": "UC-II",
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

    @field_validator("eseal_number", "origin_stream")
    @classmethod
    def _v_text(cls, v: Optional[str]) -> Optional[str]:
        return _clean_text(v)


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
    eseal_status: Optional[ESealStatus] = None
    eseal_number: Optional[str] = Field(default=None, max_length=64)
    pre_document_status: Optional[PreDocumentStatus] = None
    origin_stream: Optional[str] = Field(default=None, max_length=50, alias="originStream")

    model_config = ConfigDict(populate_by_name=True, json_schema_extra={
        "example": {"customs_status": "CLEARED", "is_released": True,
                    "yard_block": "B-04", "eseal_status": "ARMED",
                    "pre_document_status": "COMPLETED"}
    })

    @field_validator("vehicle_number")
    @classmethod
    def _v_vehicle(cls, v: Optional[str]) -> Optional[str]:
        return _clean_vehicle(v)

    @field_validator("eseal_number", "origin_stream")
    @classmethod
    def _v_text(cls, v: Optional[str]) -> Optional[str]:
        return _clean_text(v)


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
    eseal_status: Optional[str] = None
    eseal_number: Optional[str] = None
    pre_document_status: Optional[str] = None
    origin_stream: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class YardAssignmentIn(BaseModel):
    """Body for PUT /api/cargo/{container_number}/yard-assignment — the block to
    park the container in. Format-checked so an invalid block is a 400."""
    yard_block: str = Field(..., max_length=50, description="Yard block id, e.g. 'A-01'")

    model_config = ConfigDict(json_schema_extra={"example": {"yard_block": "A-01"}})

    @field_validator("yard_block")
    @classmethod
    def _v_yard_block(cls, v: str) -> str:
        return _clean_yard_block(v)


class YardAssignmentOut(BaseModel):
    """Compact yard-assignment confirmation returned to the Cargo Twin (POC-2)."""
    container_number: str
    yard_block: str
    status: str = "ASSIGNED"

    model_config = ConfigDict(json_schema_extra={
        "example": {"container_number": "GESU5123996", "yard_block": "A-01",
                    "status": "ASSIGNED"}
    })


class CargoEventOut(BaseModel):
    """One cargo lifecycle event from the notifications log (GET /api/cargo/events).

    ``event`` is a dotted topic (``cargo.created`` | ``cargo.released`` |
    ``cargo.yard_assigned`` | ``cargo.status_changed`` | ``cargo.gate_movement`` |
    ``cargo.updated`` | ``cargo.deleted``). ``id`` is a monotonic cursor UC-2
    advances via the ``since`` query param; ``payload`` carries event-specific
    detail (the changed fields)."""
    id: int
    event: str
    container_number: str
    timestamp: datetime
    payload: dict = Field(default_factory=dict)

    model_config = ConfigDict(json_schema_extra={
        "example": {"id": 42, "event": "cargo.released",
                    "container_number": "GESU5123996",
                    "timestamp": "2026-07-13T10:00:00Z",
                    "payload": {"is_released": True}}
    })


# ------------------------------------------------- POC-2 extension DTOs / enums
class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class WorkflowAction(str, Enum):
    TRIGGER = "TRIGGER"
    APPROVE = "APPROVE"
    REJECT = "REJECT"


# Yard block LETTER-zone (e.g. 'B', 'AB') — the preferred_block on a yard plan. A
# slot number is appended by the service (-> 'B-09'), so only the zone is given.
_YARD_ZONE_RE = re.compile(r"^[A-Z]{1,2}$")


def _clean_yard_zone(value: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError("preferred_block is required")
    norm = str(value).strip().upper().replace(" ", "")
    if not _YARD_ZONE_RE.match(norm):
        raise ValueError("invalid preferred_block (expected a letter zone, e.g. 'B')")
    return norm


# ---- 1. Stakeholder notifications --------------------------------------------
class NotificationCreate(BaseModel):
    container_number: str = Field(..., description="ISO-6346 container number")
    notification_type: str = Field(..., max_length=64, description="e.g. 'CUSTOMS_ALERT'")
    severity: Severity = Field(default=Severity.MEDIUM)
    message: Optional[str] = Field(default=None, max_length=1000)
    stakeholders: list[str] = Field(default_factory=list,
                                    description="Recipient roles, e.g. ['operator','customs']")

    model_config = ConfigDict(json_schema_extra={"example": {
        "container_number": "GESU5123996", "notification_type": "CUSTOMS_ALERT",
        "severity": "HIGH", "message": "Container pending customs approval",
        "stakeholders": ["operator", "customs", "control_room"]}})

    @field_validator("container_number")
    @classmethod
    def _v_container(cls, v: str) -> str:
        return _clean_container_no(v)

    @field_validator("notification_type")
    @classmethod
    def _v_type(cls, v: str) -> str:
        norm = str(v).strip().upper().replace(" ", "_") if v else ""
        if not norm:
            raise ValueError("notification_type is required")
        return norm

    @field_validator("stakeholders")
    @classmethod
    def _v_stakeholders(cls, v: list[str]) -> list[str]:
        return [s.strip() for s in (v or []) if s and s.strip()]


class NotificationCreateOut(BaseModel):
    notification_id: int
    status: str = "CREATED"

    model_config = ConfigDict(json_schema_extra={
        "example": {"notification_id": 1, "status": "CREATED"}})


class NotificationOut(BaseModel):
    notification_id: int
    container_number: str
    notification_type: str
    severity: str
    message: Optional[str] = None
    stakeholders: list[str] = Field(default_factory=list)
    status: str
    created_at: datetime


# ---- 2. Workflow --------------------------------------------------------------
class WorkflowIn(BaseModel):
    action: WorkflowAction
    comment: Optional[str] = Field(default=None, max_length=1000)

    model_config = ConfigDict(json_schema_extra={
        "example": {"action": "APPROVE", "comment": "Customs cleared"}})


class WorkflowOut(BaseModel):
    container_number: str
    workflow_status: str

    model_config = ConfigDict(json_schema_extra={
        "example": {"container_number": "GESU5123996", "workflow_status": "APPROVED"}})


class WorkflowHistoryOut(BaseModel):
    id: int
    container_number: str
    action: str
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    comment: Optional[str] = None
    created_at: datetime


# ---- 3. Yard planning ---------------------------------------------------------
class YardPlanIn(BaseModel):
    container_number: str
    preferred_block: str = Field(..., description="Yard block letter-zone, e.g. 'B'")
    priority: Priority = Field(default=Priority.MEDIUM)

    model_config = ConfigDict(json_schema_extra={"example": {
        "container_number": "GESU5123996", "preferred_block": "B", "priority": "HIGH"}})

    @field_validator("container_number")
    @classmethod
    def _v_container(cls, v: str) -> str:
        return _clean_container_no(v)

    @field_validator("preferred_block")
    @classmethod
    def _v_block(cls, v: str) -> str:
        return _clean_yard_zone(v)


class YardPlanOut(BaseModel):
    container_number: str
    assigned_block: str
    status: str = "PLANNED"

    model_config = ConfigDict(json_schema_extra={"example": {
        "container_number": "GESU5123996", "assigned_block": "B-09", "status": "PLANNED"}})


# ---- 4. Yard optimization -----------------------------------------------------
class YardOptimizationOut(BaseModel):
    yard_congestion: float
    recommendations: list[dict] = Field(default_factory=list)
    priority_containers: list[str] = Field(default_factory=list)
    busiest_block: Optional[str] = None

    model_config = ConfigDict(json_schema_extra={"example": {
        "yard_congestion": 0.72,
        "recommendations": [{"container_number": "GESU5123996", "action": "MOVE",
                             "reason": "reduce congestion"}],
        "priority_containers": ["GESU5123996"], "busiest_block": "B"}})


# ---- 5. Rake planning ---------------------------------------------------------
class RakePlanIn(BaseModel):
    rake_id: str = Field(..., max_length=64)
    containers: list[str] = Field(default_factory=list)

    model_config = ConfigDict(json_schema_extra={"example": {
        "rake_id": "RKE001", "containers": ["GESU5123996", "OOLU9050118"]}})

    @field_validator("rake_id")
    @classmethod
    def _v_rake(cls, v: str) -> str:
        norm = str(v).strip().upper().replace(" ", "") if v else ""
        if not norm:
            raise ValueError("rake_id is required")
        return norm

    @field_validator("containers")
    @classmethod
    def _v_containers(cls, v: list[str]) -> list[str]:
        return [_clean_container_no(c) for c in (v or [])]


class RakePlanOut(BaseModel):
    rake_id: str
    planned_containers: int
    status: str = "PLANNED"

    model_config = ConfigDict(json_schema_extra={"example": {
        "rake_id": "RKE001", "planned_containers": 2, "status": "PLANNED"}})


class RakePlanRecordOut(BaseModel):
    id: int
    rake_id: str
    containers: list[str] = Field(default_factory=list)
    planned_containers: int
    status: str
    created_at: datetime


# ---- 6. Reefer planning -------------------------------------------------------
class ReeferPlanIn(BaseModel):
    container_number: str
    temperature: Optional[float] = Field(default=None, description="Set-point, degrees C")
    power_required: bool = Field(default=True)

    model_config = ConfigDict(json_schema_extra={"example": {
        "container_number": "MSCU7789010", "temperature": 4, "power_required": True}})

    @field_validator("container_number")
    @classmethod
    def _v_container(cls, v: str) -> str:
        return _clean_container_no(v)


class ReeferPlanOut(BaseModel):
    container_number: str
    slot: str
    status: str = "ALLOCATED"

    model_config = ConfigDict(json_schema_extra={"example": {
        "container_number": "MSCU7789010", "slot": "REEFER-A12", "status": "ALLOCATED"}})


_ERROR_RESPONSES = {
    400: {"description": "Validation error (bad ISO-6346 / enum / types)"},
    404: {"description": "Container not found"},
    409: {"description": "Duplicate container_number"},
    500: {"description": "Internal error"},
}


def _to_event_out(row: dict) -> CargoEventOut:
    """Map a stored cargo_events row to the API shape (created_at -> timestamp)."""
    payload = row.get("payload")
    return CargoEventOut(
        id=int(row["id"]),
        event=row["event"],
        container_number=row["container_number"],
        timestamp=row["created_at"],
        payload=payload if isinstance(payload, dict) else {},
    )


def _to_out(row: dict) -> CargoOut:
    return CargoOut(**row)


def _as_list(value: object) -> list:
    """Coerce a jsonb column to a Python list. asyncpg usually parses jsonb into a
    list already; a fake repo may hand back a list; a raw string is decoded."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json as _json
        try:
            parsed = _json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _to_notification_out(row: dict) -> NotificationOut:
    return NotificationOut(
        notification_id=int(row["id"]), container_number=row["container_number"],
        notification_type=row["notification_type"], severity=row["severity"],
        message=row.get("message"), stakeholders=_as_list(row.get("stakeholders")),
        status=row["status"], created_at=row["created_at"])


def _to_rake_record_out(row: dict) -> RakePlanRecordOut:
    return RakePlanRecordOut(
        id=int(row["id"]), rake_id=row["rake_id"],
        containers=_as_list(row.get("containers")),
        planned_containers=int(row["planned_containers"]), status=row["status"],
        created_at=row["created_at"])


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
            summary="List cargo records (filter + paginate + role scope)")
async def list_cargo(
    request: Request,
    response: Response,
    container_number: Optional[str] = Query(default=None, description="Exact ISO-6346 match"),
    customs_status: Optional[CustomsStatus] = Query(default=None),
    yard_block: Optional[str] = Query(default=None),
    is_released: Optional[bool] = Query(default=None),
    vehicle_number: Optional[str] = Query(default=None),
    eseal_status: Optional[ESealStatus] = Query(default=None),
    pre_document_status: Optional[PreDocumentStatus] = Query(default=None),
    origin_stream: Optional[str] = Query(default=None, description="Cargo source stream, e.g. 'UC-II'"),
    role: Optional[str] = Query(default=None,
                                description="Scope results to a user role (e.g. 'operator', 'customs', 'driver')"),
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
        eseal_status=eseal_status.value if eseal_status else None,
        pre_document_status=pre_document_status.value if pre_document_status else None,
        origin_stream=_clean_text(origin_stream),
    )
    # Role-based filtering: prefer the AUTHENTICATED principal's role (so a query
    # param can never widen a token's scope); fall back to the ?role= param for the
    # open dev/demo profile. The role's scope OVERRIDES client filters (hard scope).
    principal = getattr(request.state, "principal", None)
    effective_role = getattr(principal, "role", None) or role
    filters.update(scope_filters_for_role(effective_role))
    rows = await service.list_cargo(**filters, limit=limit, offset=offset)
    # Total (pre-pagination) count so a paginated Cargo-Twin UI can render controls.
    response.headers["X-Total-Count"] = str(await service.count_cargo(**filters))
    return [_to_out(r) for r in rows]


@router.get("/events", response_model=list[CargoEventOut],
            summary="Poll cargo lifecycle events (notifications contract)")
async def list_cargo_events(
    response: Response,
    container_number: Optional[str] = Query(default=None, description="Only events for this container"),
    event: Optional[str] = Query(default=None, description="Only this event type, e.g. 'cargo.released'"),
    since: Optional[int] = Query(default=None, ge=0, description="Only events with id greater than this cursor"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: CargoService = Depends(get_service),
) -> list[CargoEventOut]:
    """Newest-first cargo lifecycle events for the UC-2 notifications poll. UC-2
    tracks the largest ``id`` it has seen and passes it as ``since`` to fetch only
    what is new. ``container_number`` is normalised so a spaced/lower-case value
    still matches the stored canonical form."""
    cn = _require_container_no(container_number) if container_number else None
    rows = await service.list_events(container_number=cn, event=event,
                                     since_id=since, limit=limit, offset=offset)
    if rows:
        response.headers["X-Cargo-Event-Cursor"] = str(max(int(r["id"]) for r in rows))
    return [_to_event_out(r) for r in rows]


# ======================================================= POC-2 extension endpoints
# Stakeholder notifications, workflow lifecycle, and yard/rake/reefer planning —
# all ADDITIVE to the CRUD contract above. The static-path GET routes here MUST be
# declared BEFORE GET /{container_number} (below) or the router would parse
# 'notifications' / 'yard-optimization' / 'rake-planning' as a container number.

# ---- 1. Stakeholder notifications --------------------------------------------
@router.post("/notifications", response_model=NotificationCreateOut,
             status_code=status.HTTP_201_CREATED, responses=_ERROR_RESPONSES,
             summary="Create a stakeholder notification event")
async def create_notification(
    body: NotificationCreate,
    service: CargoService = Depends(get_service),
) -> NotificationCreateOut:
    row = await service.create_notification(
        container_number=body.container_number, notification_type=body.notification_type,
        severity=body.severity.value, message=body.message, stakeholders=body.stakeholders)
    return NotificationCreateOut(notification_id=int(row["id"]),
                                 status=row.get("status", "CREATED"))


@router.get("/notifications", response_model=list[NotificationOut],
            summary="List stakeholder notifications (filter: container/type/severity/status)")
async def list_notifications(
    container_number: Optional[str] = Query(default=None),
    notification_type: Optional[str] = Query(default=None),
    severity: Optional[Severity] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: CargoService = Depends(get_service),
) -> list[NotificationOut]:
    cn = _require_container_no(container_number) if container_number else None
    ntype = notification_type.strip().upper().replace(" ", "_") if notification_type else None
    rows = await service.list_notifications(
        container_number=cn, notification_type=ntype,
        severity=severity.value if severity else None,
        status=status_filter, limit=limit, offset=offset)
    return [_to_notification_out(r) for r in rows]


# ---- 3. Yard planning ---------------------------------------------------------
@router.post("/yard-planning", response_model=YardPlanOut,
             status_code=status.HTTP_201_CREATED, responses=_ERROR_RESPONSES,
             summary="Plan a yard slot for a container in a preferred block")
async def create_yard_planning(
    body: YardPlanIn,
    service: CargoService = Depends(get_service),
) -> YardPlanOut:
    row = await service.plan_yard(container_number=body.container_number,
                                  preferred_block=body.preferred_block,
                                  priority=body.priority.value)
    return YardPlanOut(container_number=row["container_number"],
                       assigned_block=row["assigned_block"],
                       status=row.get("status", "PLANNED"))


# ---- 4. Yard operation optimization ------------------------------------------
@router.get("/yard-optimization", response_model=YardOptimizationOut,
            summary="Yard congestion score + move recommendations")
async def yard_optimization(
    service: CargoService = Depends(get_service),
) -> YardOptimizationOut:
    return YardOptimizationOut(**await service.optimize_yard())


# ---- 5. Rake planning ---------------------------------------------------------
@router.post("/rake-planning", response_model=RakePlanOut,
             status_code=status.HTTP_201_CREATED, responses=_ERROR_RESPONSES,
             summary="Plan a rail rake over a set of containers")
async def create_rake_planning(
    body: RakePlanIn,
    service: CargoService = Depends(get_service),
) -> RakePlanOut:
    row = await service.plan_rake(rake_id=body.rake_id, containers=body.containers)
    return RakePlanOut(rake_id=row["rake_id"],
                       planned_containers=int(row["planned_containers"]),
                       status=row.get("status", "PLANNED"))


@router.get("/rake-planning", response_model=list[RakePlanRecordOut],
            summary="List rake plans (optional rake_id filter)")
async def list_rake_planning(
    rake_id: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: CargoService = Depends(get_service),
) -> list[RakePlanRecordOut]:
    rid = rake_id.strip().upper().replace(" ", "") if rake_id else None
    rows = await service.list_rake_plans(rake_id=rid, limit=limit, offset=offset)
    return [_to_rake_record_out(r) for r in rows]


# ---- 6. Reefer planning -------------------------------------------------------
@router.post("/reefer-planning", response_model=ReeferPlanOut,
             status_code=status.HTTP_201_CREATED, responses=_ERROR_RESPONSES,
             summary="Allocate a powered reefer slot for a container")
async def create_reefer_planning(
    body: ReeferPlanIn,
    service: CargoService = Depends(get_service),
) -> ReeferPlanOut:
    row = await service.plan_reefer(container_number=body.container_number,
                                    temperature=body.temperature,
                                    power_required=body.power_required)
    return ReeferPlanOut(container_number=row["container_number"], slot=row["slot"],
                         status=row.get("status", "ALLOCATED"))


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


@router.put("/{container_number}/yard-assignment", response_model=YardAssignmentOut,
            responses=_ERROR_RESPONSES, summary="Assign a container to a yard block")
async def assign_yard(
    container_number: str,
    body: YardAssignmentIn,
    service: CargoService = Depends(get_service),
) -> YardAssignmentOut:
    """Persist ``jnpa.cargo.yard_block`` for one container and confirm the
    assignment. Reuses the same CargoService.update_cargo path as the generic
    PUT — no separate yard table/service. 404 if the container is unknown; 400
    (via the shared validation handler) if the block is missing/malformed."""
    cn = _require_container_no(container_number)
    try:
        row = await service.update_cargo(cn, {"yard_block": body.yard_block})
    except CargoNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found", "container_number": cn})
    return YardAssignmentOut(container_number=row["container_number"],
                             yard_block=row["yard_block"], status="ASSIGNED")


# ---- 2. Workflow lifecycle (TRIGGER -> APPROVE / REJECT) ----------------------
@router.post("/{container_number}/workflow", response_model=WorkflowOut,
             responses=_ERROR_RESPONSES, summary="Advance a container's workflow")
async def cargo_workflow(
    container_number: str,
    body: WorkflowIn,
    service: CargoService = Depends(get_service),
) -> WorkflowOut:
    """Apply a workflow action and return the new status. 404 if the container is
    unknown; the transition is also written to the append-only workflow history."""
    cn = _require_container_no(container_number)
    row = await service.apply_workflow(cn, body.action.value, body.comment)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "not_found", "container_number": cn})
    return WorkflowOut(container_number=cn, workflow_status=row["new_status"])


@router.get("/{container_number}/workflow/history",
            response_model=list[WorkflowHistoryOut], responses=_ERROR_RESPONSES,
            summary="Append-only workflow transition history for a container")
async def cargo_workflow_history(
    container_number: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: CargoService = Depends(get_service),
) -> list[WorkflowHistoryOut]:
    cn = _require_container_no(container_number)
    rows = await service.list_workflow_history(cn, limit=limit, offset=offset)
    return [WorkflowHistoryOut(**r) for r in rows]


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
