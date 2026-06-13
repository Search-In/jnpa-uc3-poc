"""/api/kpi — materialised KPI views from Timescale + degradation telemetry.

    GET /api/kpi               -> summary KPIs (reads the materialised views)
    GET /api/kpi/{view}        -> one named KPI view's rows
    GET /api/kpi/sources       -> {source, state, last_ok, latency_p95} table
                                  (the dashboard "System Health" panel)
    GET /api/kpi/cameras       -> per-camera ANPR degradation level

The KPI views are created in infra/postgres/init.sql (continuous aggregates /
plain views named jnpa.kpi_*). The endpoint reads whichever exist and degrades
to an empty list for any that don't (so the route is robust across volumes
created before this PoC stage).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from .anpr import KNOWN_CAMERAS, camera_state

log = get_logger("gateway.kpi")

router = APIRouter(prefix="/api/kpi", tags=["kpi"])

# Whitelisted KPI views (schema-qualified). The name segment in /api/kpi/{view}
# is validated against these keys so the path can never inject arbitrary SQL.
KPI_VIEWS: Dict[str, str] = {
    "throughput": "jnpa.kpi_gate_throughput",
    "dwell": "jnpa.kpi_gate_dwell",
    "anpr_hourly": "jnpa.kpi_anpr_hourly",
    "corridor_speed": "jnpa.kpi_corridor_speed",
    "alerts_by_kind": "jnpa.kpi_alerts_by_kind",
    "provisional_open": "jnpa.kpi_provisional_open",
}


async def _read_view(state: GatewayState, view_sql: str, limit: int = 500) -> List[dict]:
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(f"SELECT * FROM {view_sql} LIMIT {int(limit)}",
                               dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # view may not exist on an old volume
        log.debug("kpi_view_unavailable", view=view_sql, error=str(exc))
        return []
    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return out


@router.get("")
@router.get("/")
async def kpi_summary(state: GatewayState = Depends(get_state)) -> dict:
    """All KPI views in one payload for the dashboard summary."""
    out: Dict[str, Any] = {}
    for name, view_sql in KPI_VIEWS.items():
        out[name] = await _read_view(state, view_sql)
    REQUESTS.labels("kpi", "ok").inc()
    return {"views": out}


@router.get("/sources")
async def kpi_sources(state: GatewayState = Depends(get_state)) -> dict:
    """System-Health table: {source, state, last_ok, latency_p95} per source."""
    table = []
    for h in state.sources.table():
        table.append({
            "source": h.source,
            "state": h.state.value,
            "last_ok": h.last_ok.isoformat() if h.last_ok else None,
            "latency_p95_ms": h.latency_p95_ms,
            "last_decision_path": h.last_decision_path,
        })
    REQUESTS.labels("kpi", "ok").inc()
    return {"sources": table, "count": len(table)}


@router.get("/cameras")
async def kpi_cameras(state: GatewayState = Depends(get_state)) -> dict:
    """Per-camera degradation level (LIVE / CACHED / SYNTHETIC)."""
    rows = []
    for cam in KNOWN_CAMERAS:
        cs = camera_state(state, cam)
        rows.append({k: v for k, v in cs.items() if not k.startswith("_")})
    REQUESTS.labels("kpi", "ok").inc()
    return {"cameras": rows, "count": len(rows)}


@router.get("/{view}")
async def kpi_view(view: str, state: GatewayState = Depends(get_state)) -> dict:
    if view not in KPI_VIEWS:
        raise HTTPException(status_code=404,
                            detail={"error": "unknown_view", "view": view,
                                    "known": list(KPI_VIEWS)})
    rows = await _read_view(state, KPI_VIEWS[view])
    REQUESTS.labels("kpi", "ok").inc()
    return {"view": view, "rows": rows, "count": len(rows)}
