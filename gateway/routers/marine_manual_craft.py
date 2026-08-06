"""Manual craft assignment endpoints — committing fleet craft to a movement.

Additive: new paths only. No existing marine endpoint, model or field is touched, and
nothing here writes to core.port_craft — the imported fleet register is read-only to this
router.

Body parsing deliberately mirrors marine_manual_pilot: the JSON is read from the request
rather than gated on Content-Type, because a client that merges the header under two
casings sends `application/json, application/json`, which FastAPI parses as text/plain and
then rejects a perfectly well-formed payload with 422.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.marine.manual_craft import (LADDER, ManualCraftAssignment,
                                          ManualCraftService)

router = APIRouter(prefix="/api/marine", tags=["marine"])

_service = ManualCraftService()


class ManualCraftAssignmentOut(BaseModel):
    """One craft commitment. `active` carries the precedence flag."""
    model_config = ConfigDict(extra="ignore")
    id: int
    call_id: int
    craft_id: int
    status: str
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    craft_name: Optional[str] = None
    craft_type: Optional[str] = None
    assigned_at: Optional[datetime] = None
    dispatched_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    assisting_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    active: bool = True
    superseded_at: Optional[datetime] = None


class ManualCraftAssignmentIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_id: int = Field(..., description="Vessel call this craft is committed to")
    craft_id: int = Field(..., description="core.port_craft.craft_id from the fleet register")
    vcn: Optional[str] = None
    via_no: Optional[str] = None
    vessel_name: Optional[str] = None
    craft_name: Optional[str] = None
    craft_type: Optional[str] = None
    created_by: Optional[str] = None


class ManualCraftPage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[ManualCraftAssignmentOut]
    total: int


def _out(a: ManualCraftAssignment) -> dict[str, Any]:
    return {
        "id": a.id, "call_id": a.call_id, "craft_id": a.craft_id, "status": a.status,
        "vcn": a.vcn, "via_no": a.via_no, "vessel_name": a.vessel_name,
        "craft_name": a.craft_name, "craft_type": a.craft_type,
        "assigned_at": a.assigned_at, "dispatched_at": a.dispatched_at,
        "arrived_at": a.arrived_at, "assisting_at": a.assisting_at,
        "released_at": a.released_at, "created_by": a.created_by,
        "created_at": a.created_at, "updated_at": a.updated_at,
        "active": a.active, "superseded_at": a.superseded_at,
    }


async def _assignment_body(request: Request) -> ManualCraftAssignmentIn:
    """Parse and validate the JSON body without gating on the Content-Type header."""
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
        return ManualCraftAssignmentIn.model_validate(data)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**e, "loc": ("body", *e.get("loc", ()))} for e in exc.errors()])


@router.get("/manual-craft-assignment", response_model=ManualCraftPage,
            summary="List manual craft assignments")
async def list_assignments(
    active: Optional[bool] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items, total = await _service.list(active=active, limit=limit, offset=offset)
    return {"items": [_out(a) for a in items], "total": total}


@router.post("/manual-craft-assignment", response_model=ManualCraftAssignmentOut,
             status_code=status.HTTP_201_CREATED,
             summary="Commit a fleet craft to a movement",
             openapi_extra={"requestBody": {
                 "required": True,
                 "content": {"application/json": {
                     "schema": ManualCraftAssignmentIn.model_json_schema()}}}})
async def assign(body: ManualCraftAssignmentIn = Depends(_assignment_body)) -> dict[str, Any]:
    a = await _service.assign(
        call_id=body.call_id, craft_id=body.craft_id, vcn=body.vcn, via_no=body.via_no,
        vessel_name=body.vessel_name, craft_name=body.craft_name,
        craft_type=body.craft_type, created_by=body.created_by)
    if a is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail="Craft is already committed to a movement")
    return _out(a)


@router.patch("/manual-craft-assignment/{assignment_id}/{transition}",
              response_model=ManualCraftAssignmentOut,
              summary="Advance a commitment: dispatch | arrive | assist | release")
async def advance(assignment_id: int, transition: str) -> dict[str, Any]:
    """One route for the whole dispatch ladder.

    A route per rung would mean four near-identical handlers and a fifth to add later;
    the ladder is already declared once in the service, so the transition is data here.
    """
    target = {"dispatch": "Dispatched", "arrive": "On Scene",
              "assist": "Assisting", "release": "Released"}.get(transition)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown transition '{transition}'. Expected one of: "
                   "dispatch, arrive, assist, release")
    a = await _service.advance(assignment_id, target)
    if a is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Assignment is not live, or cannot advance to '{target}' "
                   f"from its current state (ladder: {' -> '.join(LADDER)})")
    return _out(a)
