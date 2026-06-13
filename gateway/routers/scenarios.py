"""/api/scenarios — proxy to the scenario driver (Prompt 9).

The scenario service is built in a later PoC stage (Prompt 9). Until it is up,
the gateway exposes the same surface but degrades to reading/writing
``jnpa.scenarios`` directly so the route exists and the dashboard can list and
start demo scenarios. Once the dedicated service is running on
``GATEWAY_SCENARIOS_URL`` the gateway forwards to it transparently.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, Request

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
    # Degrade: read jnpa.scenarios.
    rows = await _db_scenarios(state)
    REQUESTS.labels("scenarios", "ok").inc()
    return {"source": "db", "scenarios": rows}


async def _db_scenarios(state: GatewayState) -> list:
    from jnpa_shared.db import fetch_all
    try:
        rows = await fetch_all(
            "SELECT id, name, started_at, ended_at, params FROM jnpa.scenarios ORDER BY started_at DESC NULLS LAST",
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
