"""/api/scenarios — proxy to the scenario driver (Prompt 9).

The scenario service is built in a later PoC stage (Prompt 9). Until it is up,
the gateway exposes the same surface but degrades to reading/writing
``core.scenario`` directly so the route exists and the dashboard can list and
start demo scenarios. Once the dedicated service is running on
``GATEWAY_SCENARIOS_URL`` the gateway forwards to it transparently.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.scenarios")

router = APIRouter(prefix="/api/scenarios", tags=["scenarios"])


async def _proxy(state: GatewayState, method: str, path: str, body: Any = None):
    """Forward to the scenario service; return (ok, json) — ok False if down."""
    url = state.cfg.scenarios_url.rstrip("/") + path
    try:
        resp = await state.http.request(method, url, json=body)
    except httpx.HTTPError as exc:
        log.debug("scenarios_upstream_unreachable", url=url, error=str(exc))
        return False, None
    if resp.status_code < 400:
        try:
            return True, resp.json()
        except ValueError:
            return True, {}
    return False, None


@router.get("")
@router.get("/")
async def list_scenarios(state: GatewayState = Depends(get_state)) -> dict:
    ok, data = await _proxy(state, "GET", "/scenarios")
    if ok:
        REQUESTS.labels("scenarios", "ok").inc()
        return {"source": "scenarios", "scenarios": data}
    # Degrade: read core.scenario.
    rows = await _db_scenarios(state)
    REQUESTS.labels("scenarios", "ok").inc()
    return {"source": "db", "scenarios": rows}


@router.post("/{name}/run")
async def run_scenario(name: str, params: Dict[str, Any] = Body(default_factory=dict),
                       state: GatewayState = Depends(get_state)) -> dict:
    """Proxy a scenario run to the scenarios-runner (What-If Console trigger)."""
    ok, data = await _proxy(state, "POST", f"/scenarios/{name}/run", params or {})
    if not ok:
        raise HTTPException(status_code=502,
                            detail={"error": "scenarios_runner_unreachable", "name": name})
    REQUESTS.labels("scenarios", "ok").inc()
    return data


@router.post("/{name}/reset")
async def reset_scenario(name: str, body: Dict[str, Any] = Body(default_factory=dict),
                         state: GatewayState = Depends(get_state)) -> dict:
    ok, data = await _proxy(state, "POST", f"/scenarios/{name}/reset", body or {})
    if not ok:
        raise HTTPException(status_code=502,
                            detail={"error": "scenarios_runner_unreachable", "name": name})
    REQUESTS.labels("scenarios", "ok").inc()
    return data


@router.get("/handles")
async def list_scenario_handles(limit: int = 50,
                                state: GatewayState = Depends(get_state)) -> dict:
    """List recent scenario RUN handles (core.scenario_handle) for the What-If
    demo picker — including seeded demo timelines. Read-only; does not touch the
    live simulation flow (which mints its own handles on run)."""
    from jnpa_shared.db import fetch_all

    try:
        rows = await fetch_all(
            """
            SELECT h.handle_id, h.name, h.status, h.trace_id, h.started_at, h.ended_at,
                   (SELECT count(*) FROM core.scenario_step s WHERE s.handle_id = h.handle_id) AS step_count
            FROM core.scenario_handle h
            ORDER BY h.started_at DESC NULLS LAST
            LIMIT :limit
            """,
            {"limit": max(1, min(int(limit), 200))},
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("scenario_handles_db_unavailable", error=str(exc))
        return {"count": 0, "handles": []}
    out = []
    for r in rows:
        d = dict(r)
        for f in ("started_at", "ended_at"):
            if isinstance(d.get(f), datetime):
                d[f] = d[f].isoformat()
        d["is_demo"] = str(d.get("handle_id", "")).startswith("demo-")
        out.append(d)
    REQUESTS.labels("scenarios", "ok").inc()
    return {"count": len(out), "handles": out}


async def _db_timeline(state: GatewayState, handle_id: str) -> dict | None:
    """Read a run timeline straight from RDS (core.scenario_handle + steps).
    Powers demo/seeded timelines the live service never minted, and backstops a
    down scenario service. Returns None if the handle isn't in RDS."""
    from jnpa_shared.db import fetch_all, fetch_one

    try:
        head = await fetch_one(
            "SELECT handle_id, name, status, trace_id FROM core.scenario_handle WHERE handle_id = :h",
            {"h": handle_id}, dsn=state.cfg.postgres_dsn,
        )
        rows = await fetch_all(
            """
            SELECT handle_id, step_no, ts, title, status, trigger, detail
            FROM core.scenario_step WHERE handle_id = :h ORDER BY step_no
            """,
            {"h": handle_id}, dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("scenario_timeline_db_unavailable", error=str(exc))
        return None
    if not head and not rows:
        return None
    steps = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("ts"), datetime):
            d["ts"] = d["ts"].isoformat()
        d["scenario"] = (head or {}).get("name")
        d["trace_id"] = (head or {}).get("trace_id")
        steps.append(d)
    return {"handle_id": handle_id, "name": (head or {}).get("name"),
            "status": (head or {}).get("status"),
            "trace_id": (head or {}).get("trace_id"), "steps": steps}


@router.get("/handle/{handle_id}/timeline")
async def scenario_timeline(handle_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Timeline for a run (survives a page reload). Prefers the scenario service;
    falls back to reading the timeline straight from RDS so seeded/demo timelines
    and reloads work even when the service doesn't hold the handle in memory."""
    ok, data = await _proxy(state, "GET", f"/scenarios/{handle_id}/timeline")
    if ok:
        REQUESTS.labels("scenarios", "ok").inc()
        return data
    # Fallback: RDS-backed timeline (demo handles + service-down resilience).
    db = await _db_timeline(state, handle_id)
    if db is not None:
        REQUESTS.labels("scenarios", "ok").inc()
        return db
    raise HTTPException(status_code=404,
                        detail={"error": "timeline_unavailable", "handle_id": handle_id})


async def _db_scenarios(state: GatewayState) -> list:
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(
            "SELECT id, name, started_at, ended_at, params FROM core.scenario ORDER BY started_at DESC NULLS LAST",
            dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("scenarios_db_failed", error=str(exc))
        return []
    out = []
    for r in rows:
        d: Dict[str, Any] = dict(r)
        for f in ("started_at", "ended_at"):
            if isinstance(d.get(f), datetime):
                d[f] = d[f].isoformat()
        out.append(d)
    return out
