"""/api/alerts — operational alerts, sourced from ai/anomaly + jnpa.alerts.

The behavioural anomaly detector (ai/anomaly) owns the alert pipeline; the
gateway proxies its ``/alerts/recent`` so dashboards have one place to ask. If
ai/anomaly is unreachable it degrades to reading ``jnpa.alerts`` directly
(which also carries the gateway's own PROVISIONAL_VEHICLE / ELEVATED_SCRUTINY
alerts, so those always show up even when anomaly is down).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

import httpx
from fastapi import APIRouter, Depends, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
@router.get("/")
async def recent_alerts(
    since: str = Query(default="PT1H", description="ISO-8601 duration or timestamp"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    state: GatewayState = Depends(get_state),
) -> dict:
    cfg = state.cfg
    # --- Primary: ai/anomaly /alerts/recent ---
    url = cfg.anomaly_url.rstrip("/") + "/alerts/recent"
    params: Dict[str, Any] = {"since": since}
    if kind:
        params["kind"] = kind
    try:
        resp = await state.http.get(url, params=params)
        if resp.status_code == 200:
            data = resp.json()
            alerts = data if isinstance(data, list) else data.get("alerts", [])
            REQUESTS.labels("alerts", "ok").inc()
            return {"source": "anomaly", "alerts": alerts[:limit], "count": len(alerts[:limit])}
        log.info("alerts_upstream_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("alerts_upstream_unreachable", url=url, error=str(exc))

    # --- Degrade: read jnpa.alerts directly ---
    rows = await _db_alerts(state, kind=kind, limit=limit)
    REQUESTS.labels("alerts", "ok").inc()
    return {"source": "db", "alerts": rows, "count": len(rows)}


async def _db_alerts(state: GatewayState, *, kind: str | None, limit: int) -> List[dict]:
    from jnpa_shared.db import fetch_all
    sql = """
        SELECT id, ts, kind, severity, gate_id, plate, payload, ack
        FROM jnpa.alerts
        {where}
        ORDER BY ts DESC
        LIMIT :limit
    """.format(where="WHERE kind = :kind" if kind else "")
    params: Dict[str, Any] = {"limit": limit}
    if kind:
        params["kind"] = kind
    try:
        rows = await fetch_all(sql, params, dsn=state.cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("alerts_db_failed", error=str(exc))
        return []
    out = []
    for r in rows:
        d = dict(r)
        for f in ("id", "ts"):
            if isinstance(d.get(f), (datetime,)):
                d[f] = d[f].isoformat()
            elif d.get(f) is not None and f == "id":
                d[f] = str(d[f])
        out.append(d)
    return out
