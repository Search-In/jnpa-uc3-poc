"""/api/thread — the vessel → container → truck golden thread.

One call returns a container's whole life across the nineteen tables that record
it, each hop labelled `FOUND`, `NOT_IN_CORPUS` or `ERROR`, plus the trucks it
touched and the SQL that produced every figure.

    GET /api/thread/container/{container_no}   -> the full lifecycle
    GET /api/thread/vessel                     -> every container on a vessel call
    GET /api/thread/vehicle/{plate}            -> what a truck carried

WHY THE VERDICTS MATTER. `NOT_IN_CORPUS` and `ERROR` are reported separately and
never merged. Measured on RDS 17-Aug-2026, only **42 of 11,957 containers reach a
truck by any route**, because the corpus's manifest set and its gate-document set
share no containers at all. A traversal that silently dropped empty hops would
present that 0.35 % as a complete chain — which is exactly the misrepresentation
the JNPA Notice asks bidders to surface rather than hide.

Read-only. `ContainerThreadService.assert_read_only` rejects any statement that
is not a SELECT, and every hop runs on a non-transactional connection.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from ..logging import get_logger
from ..metrics import REQUESTS
from services.thread import ContainerThreadService

log = get_logger("gateway.thread")

router = APIRouter(prefix="/api/thread", tags=["thread"])

_service: Optional[ContainerThreadService] = None


def get_service(request: Request) -> ContainerThreadService:
    global _service
    if _service is None:
        cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
        _service = ContainerThreadService(dsn=getattr(cfg, "postgres_dsn", None) or None)
    return _service


@router.get("/container/{container_no}",
            summary="Every recorded step of one container's life, with its trucks")
async def container_thread(
    container_no: str = Path(..., description="ISO 6346, e.g. DPWU9011100"),
    row_limit: int = Query(20, ge=1, le=200,
                           description="Max rows surfaced per hop"),
    svc: ContainerThreadService = Depends(get_service),
) -> Dict[str, Any]:
    """The import and export lifecycle for one box.

    Every hop is visited and reported, including the ones with nothing in them —
    a container that stops at the manifest says so, rather than appearing to have
    no history. `summary.reaches_a_vehicle` is the single flag telling you whether
    the truck half of the chain exists for this box at all.
    """
    cn = (container_no or "").strip()
    if not cn:
        raise HTTPException(status_code=400, detail={"error": "container_required"})
    res = await svc.container_thread(cn, row_limit=row_limit)
    REQUESTS.labels("thread", "ok").inc()
    return res.as_dict()


@router.get("/vessel", summary="Every container on a vessel call, from every source")
async def vessel_thread(
    vessel_name: Optional[str] = Query(None, description="e.g. XIN HANG ZHOU"),
    vcn: Optional[str] = Query(None, description="e.g. INNSA1NS0S0552"),
    via_no: Optional[str] = Query(None, description="e.g. S0552"),
    imo_no: Optional[str] = Query(None, description="e.g. 9523017"),
    limit: int = Query(500, ge=1, le=2000),
    svc: ContainerThreadService = Depends(get_service),
) -> Dict[str, Any]:
    """Resolve a call by any of its four key families and list its containers.

    The families are kept separate on purpose — a VCN, a VIA, an IMO and a name
    identify different things (a call, a call, a hull, a hull), and the corpus
    populates them inconsistently. Each contributing source is reported with its
    own count so a thin result is visibly thin rather than quietly wrong.
    """
    if not any((vessel_name, vcn, via_no, imo_no)):
        raise HTTPException(status_code=400, detail={
            "error": "key_required",
            "message": "supply at least one of vessel_name, vcn, via_no, imo_no"})
    out = await svc.vessel_containers(vessel_name=vessel_name, vcn=vcn, via_no=via_no,
                                      imo_no=imo_no, limit=limit)
    REQUESTS.labels("thread", "ok").inc()
    return out


@router.get("/vehicle/{plate}", summary="What one truck carried, and for whom")
async def vehicle_thread(
    plate: str = Path(..., description="Truck registration, e.g. MH43BX1488"),
    svc: ContainerThreadService = Depends(get_service),
) -> Dict[str, Any]:
    """Every container this plate appears against, plus its transporter/driver
    with the provenance of that mapping (`DOCUMENT_EVIDENCED` vs `SYNTHETIC`)."""
    p = (plate or "").strip().upper()
    if not p:
        raise HTTPException(status_code=400, detail={"error": "plate_required"})
    out = await svc.vehicle_thread(p)
    REQUESTS.labels("thread", "ok").inc()
    return out


@router.get("/subjects",
            summary="Containers worth offering as worked examples, computed live")
async def thread_subjects(
    limit: int = Query(5, ge=1, le=25,
                       description="How many examples to return"),
    stage: Optional[str] = Query(None,
                                 description="Restrict to IMPORT or EXPORT"),
    svc: ContainerThreadService = Depends(get_service),
) -> Dict[str, Any]:
    """Worked-example containers, derived from the database rather than listed.

    Over a corpus this disjoint most container numbers legitimately return
    nothing, and a viewer cannot tell that from a broken lookup — so a board
    that offers no examples is unusable. UC-2 previously hardcoded five, which
    was a claim about the data frozen into the frontend: after any ingest it
    could name a box whose chain no longer resolves, and nothing would say so.

    Ranked by how many JNPA DOCUMENTS name each box (never by total sources —
    the simulator tables name tens of thousands the corpus is silent about), and
    selected so that between them the examples reach every stage any document
    reaches.
    """
    return await svc.subjects(limit=limit, stage=stage)
