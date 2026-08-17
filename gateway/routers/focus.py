"""/api/focus — the cross-origin channel for the port-wide entity focus.

WHY A BACKEND HOP FOR A UI CONCERN. The three dashboards are deployed to three
different hosts (vessel-one / logistics-two / traffic-three), so `BroadcastChannel`
and `postMessage` cannot carry a selection between them — the browser isolates
them by origin. All three DO already hold an authenticated socket to this
gateway, which makes it the only channel in the estate that reaches every app.
So the focus travels: app -> POST /api/focus/broadcast -> WsHub -> every app.

This endpoint is a pure relay. It reads nothing, writes nothing, and persists
nothing; the identity keys it carries are the ones already visible on screen to
the operator who raised them. The durable copy of a focus is the URL grammar in
the client, which is why the whole feature still works with this gateway down.

    POST /api/focus/broadcast   -> fan a PortFocus out to every connected client
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.focus")

router = APIRouter(prefix="/api/focus", tags=["focus"])

_ORIGINS = {"UC-1", "UC-2", "UC-3", "SUITE"}


class PortFocus(BaseModel):
    """Mirror of `web/src/lib/focusStore.ts`. Every identity field is optional:
    a focus may be a whole vessel call, or one box, or one truck.

    The two vessel key families stay SEPARATE by design — `poc_1`'s domain model
    states an AIS `Vessel` (MMSI) and a `VesselCall` (VCN/IMO) are not joinable,
    so this shape never derives one from the other.
    """

    vcn: Optional[str] = Field(default=None, max_length=32, description="e.g. INNSA1NS0S0552")
    viaNo: Optional[str] = Field(default=None, max_length=16, description="e.g. S0552")
    imoNo: Optional[str] = Field(default=None, max_length=16, description="e.g. 9523017")
    vesselName: Optional[str] = Field(default=None, max_length=120)
    containerNo: Optional[str] = Field(default=None, max_length=20, description="ISO 6346")
    vehicleNo: Optional[str] = Field(default=None, max_length=24)
    igmNo: Optional[str] = Field(default=None, max_length=24)
    # The shared date window, inclusive both ends, YYYY-MM-DD in IST. Carried on
    # the focus so every dashboard bounds itself to the same slice of a corpus
    # whose groups do not overlap in time.
    fromDate: Optional[str] = Field(default=None, max_length=10, description="YYYY-MM-DD, IST")
    toDate: Optional[str] = Field(default=None, max_length=10,
                                  description="YYYY-MM-DD, IST, inclusive of the whole day")
    asOf: Optional[str] = Field(default=None, max_length=40, description="IST ISO-8601")
    origin: str = Field(default="UC-3", description="UC-1 | UC-2 | UC-3 | SUITE")
    nonce: int = Field(default=0, ge=0)

    model_config = {
        "json_schema_extra": {
            "example": {
                "vcn": "INNSA1NS0S0552",
                "viaNo": "S0552",
                "imoNo": "9523017",
                "vesselName": "XIN HANG ZHOU",
                "containerNo": "DPWU9011100",
                "fromDate": "2026-06-06",
                "toDate": "2026-06-12",
                "asOf": "2026-06-12T00:00:00+05:30",
                "origin": "UC-1",
                "nonce": 3,
            }
        }
    }


@router.post("/broadcast", summary="Fan a port-wide focus out to every connected client")
async def broadcast_focus(
    focus: PortFocus = Body(...),
    state: GatewayState = Depends(get_state),
) -> Dict[str, Any]:
    """Relay a focus to every live WebSocket as a `focus` frame.

    Deliberately un-addressed (no ``device_id``): a focus is a control-room
    concept, and the driver PWA ignores the frame. The publishing app suppresses
    its own echo by comparing identity fields, so a round trip cannot loop.
    """
    payload = focus.model_dump()
    if payload.get("origin") not in _ORIGINS:
        payload["origin"] = "UC-3"
    await state.ws.broadcast("focus", payload)
    REQUESTS.labels("focus", "ok").inc()
    log.info("focus_broadcast", extra={"origin": payload["origin"],
                                       "has_vessel": bool(payload.get("vcn") or payload.get("viaNo")),
                                       "has_container": bool(payload.get("containerNo"))})
    return {"delivered": True, "focus": payload}
