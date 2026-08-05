"""/api/export-chain — the export-lifecycle steps that had no read endpoint.

The canonical export order is:

    booking → Form 11 (rail) / Form 13 (road) → gate-in (CODECO 'E') → VGM & seals
    → Shipping Bill → LEO → EAL → COPRAR → COARRI → VESDEP

Most of those steps already have a home: Form 13 / EIR / PIN on `/api/gate-docs`,
the EAL on `/api/shipping-lines`, gate-in on `/api/shipping-lines/gate-movements`,
Shipping Bill and LEO on `/api/customs`, and the departure on `/api/marine/calls`
(which already returns `atd`).

Four did not, and this router adds them. All READ-ONLY; nothing here writes.

    GET /api/export-chain/form11              rail pre-advice (core.form11)
    GET /api/export-chain/load-list           COPRAR items  (core.coprar_item)
    GET /api/export-chain/load-confirmations  COARRI moves  (core.coarri_move)
    GET /api/export-chain/synthetic           SYNTHETIC end-to-end chains (schema synth)

⚠ The `synthetic` route serves schema `synth` — generated demo data, NOT customer
data. It exists because no real container in the corpus traverses the whole
chain. Every response is stamped `"synthetic": true` and carries a `notice`, so a
consumer cannot render it as real by accident. See
markdowns/04_Export_Build_Plan.md §2.6 in the dashboard repo.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from jnpa_shared.db import get_engine

from ..metrics import REQUESTS

router = APIRouter(prefix="/api/export-chain", tags=["export-chain"])


class Page(BaseModel):
    items: List[Dict[str, Any]]
    total: int
    limit: int
    offset: int
    count: int


def _dsn(request: Request) -> Optional[str]:
    cfg = getattr(getattr(request.app.state, "gw", None), "cfg", None)
    return getattr(cfg, "postgres_dsn", None) or None


async def _page(dsn: Optional[str], select: str, table: str, response: Response,
                *, order: str, limit: int, offset: int,
                where: str = "", params: Optional[dict] = None) -> Page:
    params = dict(params or {})
    async with get_engine(dsn).connect() as conn:
        total = (await conn.execute(
            text(f"SELECT count(*) FROM {table} {where}"), params)).scalar()
        params.update({"limit": limit, "offset": offset})
        rows = (await conn.execute(text(
            f"{select} FROM {table} {where} ORDER BY {order} LIMIT :limit OFFSET :offset"),
            params)).mappings().all()
    items = [dict(r) for r in rows]
    response.headers["X-Total-Count"] = str(total or 0)
    return Page(items=items, total=int(total or 0), limit=limit, offset=offset,
                count=len(items))


@router.get("/form11", response_model=Page,
            summary="Form 11 rail pre-advice (the rail-origin export step)")
async def list_form11(
    request: Request,
    response: Response,
    container: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page:
    """The rail-origin counterpart of Form 13.

    ⚠ Each source workbook holds exactly ONE row — these are templates the
    terminals supplied, not a rake's full load. `truck_no`, `driver_train_id` and
    `shipping_bill_no` exist as columns in the source but are empty in every one.
    """
    where, params = ("WHERE container_no = :cn", {"cn": container.strip().upper()}) \
        if container else ("", {})
    res = await _page(
        _dsn(request),
        "SELECT form11_id, template, visit_no, container_no, iso_code, size_ft, "
        "       booking_no, preadvice_type, trade_type, arrival_mode, origin_port, "
        "       pod, final_destination, origin_type, vgm_kg, commodity, line_code, "
        "       status, line_seal, customs_seal, extras",
        "core.form11", response, order="form11_id", limit=limit, offset=offset,
        where=where, params=params)
    REQUESTS.labels("export_chain", "ok").inc()
    return res


@router.get("/load-list", response_model=Page,
            summary="COPRAR advance load list (containers ordered for loading)")
async def list_coprar(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page:
    """⚠ The corpus COPRAR sample is Kolkata / Haldia, NOT a JNPA call. Usable to
    demonstrate the message schema; it is not JNPA traffic."""
    res = await _page(
        _dsn(request),
        "SELECT id, vcn, voyage_no, rotation_no, container_no, equipment_status, "
        "       container_status, iso_code, tare_weight, gross_weight, port_of_origin, "
        "       pol, pod, final_pod, igm_line_no, igm_subline_no, cargo_type, "
        "       imdg_class, disposal_mode, arrival_mode",
        "core.coprar_item", response, order="id", limit=limit, offset=offset)
    REQUESTS.labels("export_chain", "ok").inc()
    return res


@router.get("/load-confirmations", response_model=Page,
            summary="COARRI load / discharge confirmation (what was actually worked)")
async def list_coarri(
    request: Request,
    response: Response,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page:
    """⚠ The corpus COARRI sample is Visakhapatnam, NOT a JNPA call. Also
    incomplete: the source declares 1,107 containers but only 200 items are
    present, and 50 of those were lost because the 4th message is truncated at
    Excel's 32,767-character cell limit — so 150 rows land."""
    res = await _page(
        _dsn(request),
        "SELECT id, vcn, imo_no, terminal_code, container_no, equipment_status, "
        "       line_code, iso_code, customs_seal, shipper_seal, icd_indicator, "
        "       shipped_ts, landed_ts, berthing_ts, damage_flag, damage_desc",
        "core.coarri_move", response, order="id", limit=limit, offset=offset)
    REQUESTS.labels("export_chain", "ok").inc()
    return res


@router.get("/synthetic", summary="SYNTHETIC end-to-end export chains (schema synth)")
async def list_synthetic(
    request: Request,
    container: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """⚠⚠ GENERATED DEMO DATA — NOT CUSTOMER DATA.

    No real container in the JNPA corpus traverses the full export lifecycle: the
    document families are disjoint by design (verified — the intersection of the
    advance lists, the manifests and the gate documents is empty). These 60
    chains exist so the lifecycle can be shown end to end at all.

    Container numbers use the prefix `SYNU`, which is not an allocated BIC owner
    code, so they cannot collide with a real box. Each chain's FINAL step carries
    the REAL departure timestamp of a real vessel call, so the last link is
    verifiable against customer data.

    The response is stamped `synthetic: true` and carries `notice`. A client that
    renders this without a synthetic badge is misrepresenting it.
    """
    dsn = _dsn(request)
    params: Dict[str, Any] = {"limit": limit, "offset": offset}
    where = ""
    if container:
        where = "WHERE container_no = :cn"
        params["cn"] = container.strip().upper()
    async with get_engine(dsn).connect() as conn:
        total = (await conn.execute(text(
            f"SELECT count(*) FROM synth.export_container {where}"), params)).scalar()
        rows = (await conn.execute(text(
            "SELECT container_no, iso_code, line_code, booking_no, origin_port, "
            "       origin_type, arrival_mode, cfs_name, transporter, truck_no, pod, "
            "       vgm_kg, line_seal, customs_seal, shipping_bill_no, "
            "       shipping_bill_date, leo_no, leo_date, leo_rotation_no, "
            "       gate_pass_no, gate_no, vcn, vessel_name, via_no, departed_at "
            f"FROM synth.export_container {where} "
            "ORDER BY container_no LIMIT :limit OFFSET :offset"), params)).mappings().all()
        containers = [dict(r) for r in rows]
        events: Dict[str, list] = {}
        if containers:
            ev = (await conn.execute(text(
                "SELECT container_no, step_no, step_code, step_label, event_ts, doc_ref "
                "FROM synth.export_event WHERE container_no = ANY(:ids) "
                "ORDER BY container_no, step_no"),
                {"ids": [c["container_no"] for c in containers]})).mappings().all()
            for e in ev:
                events.setdefault(e["container_no"], []).append(dict(e))
        for c in containers:
            c["steps"] = events.get(c["container_no"], [])
    REQUESTS.labels("export_chain", "ok").inc()
    return {
        "synthetic": True,
        "notice": ("GENERATED DEMO DATA — not customer data. No real container in the "
                   "corpus traverses the full export lifecycle, so these chains were "
                   "generated to show it end to end. Container prefix SYNU is not an "
                   "allocated BIC owner code. Each chain's final step carries the REAL "
                   "departure of a real vessel call."),
        "items": containers,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
        "count": len(containers),
    }
