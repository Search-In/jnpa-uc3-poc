"""Manual pilot assignment endpoints — the operator fallback path.

Additive: this router introduces new paths only. No existing marine endpoint, response
model or field is touched, and nothing here writes to core.pilotage — the imported
pilot-memo / pilot-card workflow is untouched by every route below.

Precedence is surfaced honestly rather than silently: assigning a pilot to a call that
already has IMPORTED pilotage returns 409, because the record would be shadowed the
moment it was written.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.marine.manual_pilot import ManualPilotAssignment, ManualPilotService

router = APIRouter(prefix="/api/marine", tags=["marine"])

_service = ManualPilotService()


class ManualPilotAssignmentOut(BaseModel):
    """One manual assignment. Mirrors the table; `active` carries the precedence flag."""
    model_config = ConfigDict(extra="ignore")
    id: int
    call_id: int
    pilot_code: str
    status: str
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    pilot_name: Optional[str] = None
    assigned_at: Optional[datetime] = None
    boarded_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    #: False once imported pilotage arrived for this call. The row is kept for audit.
    active: bool = True
    superseded_at: Optional[datetime] = None


class ManualPilotAssignmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_id: int = Field(..., description="Vessel call this assignment belongs to")
    pilot_code: str = Field(..., min_length=1,
                            description="Roster code or acknowledged name from the Pilot Register")
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    pilot_name: Optional[str] = None
    created_by: Optional[str] = None


class ManualPilotPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[ManualPilotAssignmentOut]
    total: int


def _out(a: ManualPilotAssignment) -> dict[str, Any]:
    return {
        "id": a.id, "call_id": a.call_id, "pilot_code": a.pilot_code, "status": a.status,
        "vcn": a.vcn, "via_no": a.via_no, "imo_no": a.imo_no,
        "vessel_name": a.vessel_name, "pilot_name": a.pilot_name,
        "assigned_at": a.assigned_at, "boarded_at": a.boarded_at,
        "released_at": a.released_at, "created_by": a.created_by,
        "created_at": a.created_at, "updated_at": a.updated_at,
        "active": a.active, "superseded_at": a.superseded_at,
    }


@router.get("/manual-pilot-assignment", response_model=ManualPilotPage,
            summary="List manual pilot assignments")
async def list_assignments(
    active: Optional[bool] = Query(default=None,
                                   description="true = live only, false = superseded only"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items, total = await _service.list(active=active, limit=limit, offset=offset)
    return {"items": [_out(a) for a in items], "total": total}


async def _assignment_body(request: Request) -> ManualPilotAssignmentIn:
    """Parse and validate the JSON body without gating on the Content-Type header.

    WHY THIS IS NOT JUST `body: ManualPilotAssignmentIn`
    ---------------------------------------------------
    FastAPI only calls `request.json()` when the Content-Type parses to
    `application/json`. It parses that header with `email.message.Message`, which returns
    `text/plain` for anything malformed — including a DUPLICATED value such as
    `application/json, application/json`, which is what a client produces when it merges
    the header twice under different casing ('content-type' and 'Content-Type' are two
    distinct keys in a plain object, and fetch combines them).

    In that case the body is left as raw bytes and reaches the model as a `str`, so
    Pydantic reports `model_attributes_type` — "Input should be a valid dictionary or
    object to extract fields from" — and the caller sees a 422 for a payload that is
    perfectly well-formed JSON.

    Reading the body directly removes that coupling: the request is judged on its
    CONTENT, not on a header it already contradicted. Validation itself is unchanged —
    the same Pydantic model, and genuine schema violations still raise
    RequestValidationError, so the 422 body shape stays exactly what it was.
    """
    raw = await request.body()
    if not raw:
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body",), "msg": "Field required", "input": None}])
    try:
        data = json.loads(raw)
    except ValueError:
        raise RequestValidationError(
            [{"type": "json_invalid", "loc": ("body",),
              "msg": "Body is not valid JSON", "input": raw[:200].decode(errors="replace")}])
    if not isinstance(data, dict):
        raise RequestValidationError(
            [{"type": "model_attributes_type", "loc": ("body",),
              "msg": "Input should be a valid dictionary or object to extract fields from",
              "input": data}])
    try:
        return ManualPilotAssignmentIn.model_validate(data)
    except ValidationError as exc:
        # Re-raised as FastAPI's own error, with the 'body' prefix FastAPI itself adds to
        # `loc`, so the 422 payload is identical to what `body: Model` would produce.
        raise RequestValidationError(
            [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors()])


@router.post("/manual-pilot-assignment", response_model=ManualPilotAssignmentOut,
             status_code=status.HTTP_201_CREATED,
             summary="Assign a pilot to a call that has no imported pilotage",
             # Declared explicitly because the body arrives through a dependency; without
             # this the OpenAPI schema would lose the request-body documentation.
             # Declared inline rather than by $ref: the model is no longer a body
             # parameter, so FastAPI does not emit it into components/schemas and a
             # reference would dangle.
             openapi_extra={"requestBody": {
                 "required": True,
                 "content": {"application/json": {
                     "schema": ManualPilotAssignmentIn.model_json_schema()}}}})
async def assign(body: ManualPilotAssignmentIn = Depends(_assignment_body)) -> dict[str, Any]:
    a = await _service.assign(
        call_id=body.call_id, pilot_code=body.pilot_code, vcn=body.vcn,
        via_no=body.via_no, imo_no=body.imo_no, vessel_name=body.vessel_name,
        pilot_name=body.pilot_name, created_by=body.created_by)
    if a is None:
        # Either imported pilotage exists (the INSERT's WHERE NOT EXISTS refused) or a
        # live assignment already covers this call (the partial unique index refused).
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Call already has imported pilotage or a live manual assignment")
    return _out(a)


@router.patch("/manual-pilot-assignment/{assignment_id}/board",
              response_model=ManualPilotAssignmentOut,
              summary="Mark the assigned pilot as onboard")
async def board(assignment_id: int) -> dict[str, Any]:
    a = await _service.board(assignment_id)
    if a is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Assignment is not live, or is not in 'Assigned' state")
    return _out(a)


@router.patch("/manual-pilot-assignment/{assignment_id}/release",
              response_model=ManualPilotAssignmentOut,
              summary="Release the pilot from this movement")
async def release(assignment_id: int) -> dict[str, Any]:
    a = await _service.release(assignment_id)
    if a is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Assignment is not live, or is already released")
    return _out(a)
