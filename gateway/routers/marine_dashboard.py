"""/api/marine dashboard reads — UC-I operational boards (additive).

Currently hosts the 5-day berthing plan (UI-028 / UC1-024). Further dashboard
paths (berths / KPIs / vessel-states) can land here without touching the
vessel-call spine router.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.berthing_plan import BerthingPlanService

router = APIRouter(prefix="/api/marine", tags=["marine"])

_API = "marine_dashboard"
_service: Optional[BerthingPlanService] = None


def get_plan_service(request: Request) -> BerthingPlanService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = BerthingPlanService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class PlanWindowOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    start: datetime
    end: datetime
    anchor: datetime


class PlanEntryOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: str
    source: str
    berth_code: str = ""
    berth_raw: str = ""
    terminal: str = ""
    vessel_name: str = ""
    voyage_no: str = ""
    imo_no: str = ""
    shipping_line: str = ""
    status: str = ""
    start_ts: datetime
    end_ts: datetime
    end_estimated: bool = False
    ref: str = ""
    vcn: str = ""
    via_no: str = ""


class BerthingPlanOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    data_mode: str
    source: str
    observed_at: Optional[datetime] = None
    as_of: Optional[datetime] = None
    window: PlanWindowOut
    entries: List[PlanEntryOut]


@router.get("/berthing-plan", response_model=BerthingPlanOut,
            summary="5-day berthing plan — confirmed (reports) vs indicative (twin)")
async def berthing_plan(
    days: int = Query(default=5, ge=1, le=14,
                      description="Forward horizon in days (tender minimum = 5)"),
    at: Optional[datetime] = Query(
        default=None,
        description="Sim/demo pin (ISO). Omitted → latest berthing actual in corpus.",
    ),
    service: BerthingPlanService = Depends(get_plan_service),
) -> BerthingPlanOut:
    res: dict[str, Any] = await service.plan(at=at, days=days)
    REQUESTS.labels(_API, "ok").inc()
    return BerthingPlanOut(**res)
