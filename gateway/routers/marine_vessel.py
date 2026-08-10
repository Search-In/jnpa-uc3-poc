"""/api/marine/vessels — UC-I Marine vessel master register (read-only, additive).

A thin router over :class:`services.marine.vessel.VesselService`, in the same mould as
gateway/routers/marine_port_craft.py. Serves the VESPRO-sourced hull registry ingested
through the SHARED marine upload endpoints (`/api/marine/upload`) — there is NO separate
vessel upload endpoint, and this router adds no write path.

    GET /api/marine/vessels          -> list + filter
    GET /api/marine/vessels/stats    -> registry totals + completeness counters
    GET /api/marine/vessels/{imo_no} -> one hull + its P&I cover

Reads ONLY core.vessel / core.vessel_insurance. Adds no column to, and changes no response
of, the existing /api/marine/calls contract — a call keeps carrying its own vessel_name and
imo_no, and a client that never calls this router sees byte-identical behaviour.

`{imo_no}` is the natural key (text PK), so the route is declared AFTER /stats: an
unqualified string path segment would otherwise capture "stats".

RBAC: covered by the existing ("/api/marine", CONTROL_ROOM | CUSTOMS) policy in
gateway/auth.py — no new policy entry required.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from ..metrics import REQUESTS
from services.marine.vessel import VesselService

router = APIRouter(prefix="/api/marine/vessels", tags=["marine"])

_API = "marine_vessel"
_service: Optional[VesselService] = None


def get_service(request: Request) -> VesselService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = VesselService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


class InsuranceOut(BaseModel):
    """One P&I cover block (core.vessel_insurance). 0..n per hull."""
    model_config = ConfigDict(extra="ignore")
    pi_club: Optional[str] = None
    valid_until: Optional[_dt.date] = None


class VesselOut(BaseModel):
    """core.vessel, in DDL declaration order.

    Every field is Optional: VESPRO is a sparse document — TEU appears in 6 of the 9
    corpus files and MMSINumber in 2 — so an absent particular is normal, not an error.
    """
    model_config = ConfigDict(extra="ignore")
    imo_no: Optional[str] = None
    vessel_name: Optional[str] = None
    call_sign: Optional[str] = None
    flag: Optional[str] = None
    vessel_type: Optional[str] = None
    mtmv: Optional[str] = None
    loa_m: Optional[float] = None
    beam_m: Optional[float] = None
    lbp_m: Optional[float] = None
    max_draft_m: Optional[float] = None
    grt: Optional[float] = None
    nrt: Optional[float] = None
    dwt: Optional[float] = None
    teu_capacity: Optional[int] = None
    mmsi: Optional[str] = None
    engine_type: Optional[str] = None
    num_engines: Optional[int] = None
    propulsion_type: Optional[str] = None
    num_propellers: Optional[int] = None
    max_speed_kn: Optional[float] = None
    bow_thruster: Optional[bool] = None
    stern_thruster: Optional[bool] = None
    built_date: Optional[_dt.date] = None
    reg_port: Optional[str] = None
    owner_name: Optional[str] = None
    email: Optional[str] = None
    vespro_ref: Optional[str] = None
    updated_at: Optional[_dt.datetime] = None
    #: Present on the single-vessel read only; always [] on list rows.
    insurance: List[InsuranceOut] = []


class VesselListResponse(BaseModel):
    items: List[VesselOut]
    total: int
    limit: int
    offset: int
    count: int


class FlagStat(BaseModel):
    flag: Optional[str] = None
    count: int


class VesselStatsOut(BaseModel):
    total: int
    #: Hulls carrying LOA + beam + max draft — the berth-fit engine's input completeness.
    with_dimensions: int
    with_teu: int
    with_mmsi: int
    avg_loa_m: Optional[float] = None
    max_draft_m: Optional[float] = None
    by_flag: List[FlagStat]


def _filters(flag, vessel_type, name, imo, owner, call_sign,
             date_from=None, date_to=None) -> Dict[str, Any]:
    return {"flag": flag, "vessel_type": vessel_type, "name": name, "imo": imo,
            "owner": owner, "call_sign": call_sign,
            "date_from": date_from, "date_to": date_to}


@router.get("", response_model=VesselListResponse,
            summary="List / filter the UC-I vessel master register")
async def list_vessels(
    flag: Optional[str] = Query(default=None),
    vessel_type: Optional[str] = Query(default=None),
    name: Optional[str] = Query(default=None, description="vessel name contains"),
    imo: Optional[str] = Query(default=None, description="IMO contains"),
    owner: Optional[str] = Query(default=None, description="owner contains"),
    call_sign: Optional[str] = Query(default=None, description="call sign contains"),
    # Demo replay date filter (UC1-004). core.vessel is a hull REGISTER, not a port
    # visit, so there is no ETA/ATA to anchor on — updated_at (last registry sync) is
    # the one honest timestamp the table carries. Same from/to idiom as /marine/calls.
    date_from: Optional[_dt.datetime] = Query(default=None, alias="from", description="updated_at >="),
    date_to: Optional[_dt.datetime] = Query(default=None, alias="to", description="updated_at <="),
    sort: str = Query(default="vessel_name"),
    direction: str = Query(default="asc"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: VesselService = Depends(get_service),
) -> VesselListResponse:
    res = await service.list_vessels(
        _filters(flag, vessel_type, name, imo, owner, call_sign, date_from, date_to),
        sort=sort, direction=direction, limit=limit, offset=offset)
    REQUESTS.labels(_API, "ok").inc()
    return VesselListResponse(**res)


@router.get("/stats", response_model=VesselStatsOut,
            summary="Vessel-registry totals + particular completeness")
async def stats(
    date_from: Optional[_dt.datetime] = Query(default=None, alias="from", description="updated_at >="),
    date_to: Optional[_dt.datetime] = Query(default=None, alias="to", description="updated_at <="),
    service: VesselService = Depends(get_service),
) -> VesselStatsOut:
    res = await service.stats(_filters(None, None, None, None, None, None, date_from, date_to))
    REQUESTS.labels(_API, "ok").inc()
    return VesselStatsOut(**res)


@router.get("/{imo_no}", response_model=VesselOut,
            summary="One vessel (hull) + its P&I cover")
async def get_vessel(imo_no: str,
                     service: VesselService = Depends(get_service)) -> VesselOut:
    res = await service.get(imo_no)
    if res is None:
        REQUESTS.labels(_API, "not_found").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail={"error": "vessel_not_found", "imo_no": imo_no})
    REQUESTS.labels(_API, "ok").inc()
    return VesselOut(**res)
