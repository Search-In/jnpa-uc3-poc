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
from fastapi import APIRouter, Depends, HTTPException, Query

from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.alerts")

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

# NOTIF-5 governance ---------------------------------------------------------
# (1) Role filter: which alert kinds each role should see. A role only receives
#     alerts relevant to it; control-room roles see everything. Unknown kinds
#     default to "all roles" so nothing is silently dropped.
_CONTROL_ROOM = {"JNPA_TRAFFIC", "DTCCC_ADMIN", "TERMINAL_OPS"}
_KIND_ROLES: Dict[str, set[str]] = {
    "CUSTOMS_FLAG": _CONTROL_ROOM | {"CUSTOMS"},
    "PROVISIONAL_VEHICLE": _CONTROL_ROOM | {"CUSTOMS"},
    "WRONG_WAY": _CONTROL_ROOM | {"TRAFFIC_POLICE"},
    "ILLEGAL_PARKING": _CONTROL_ROOM | {"TRAFFIC_POLICE"},
    "ABANDONED": _CONTROL_ROOM | {"TRAFFIC_POLICE"},
    "ROUTE_DEVIATION": _CONTROL_ROOM | {"TRAFFIC_POLICE"},
    "ELEVATED_SCRUTINY": _CONTROL_ROOM | {"CUSTOMS", "TRAFFIC_POLICE"},
}


def _kind_roles(kind: str | None) -> set[str] | None:
    """Roles permitted to see a kind (None => visible to all)."""
    if not kind:
        return None
    return _KIND_ROLES.get(str(kind).upper())


def _role_can_see(role: str | None, kind: str | None) -> bool:
    if not role:
        return True  # no role context => unfiltered (auth-disabled / dashboard)
    allowed = _kind_roles(kind)
    return allowed is None or role in allowed


# (2) i18n: stable key per kind so clients can localize the alert label.
def _i18n_key(kind: str | None) -> str:
    return f"alertKind.{str(kind or 'ALERT').upper()}"


def _decorate(alert: dict) -> dict:
    """Attach the i18n key (multilingual support) without mutating the source."""
    return {**alert, "i18n_key": _i18n_key(alert.get("kind"))}


@router.get("")
@router.get("/")
async def recent_alerts(
    since: str = Query(default="PT1H", description="ISO-8601 duration or timestamp"),
    kind: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    role: str | None = Query(default=None, description="filter to alerts a role may see (NOTIF-5)"),
    state: GatewayState = Depends(get_state),
) -> dict:
    cfg = state.cfg

    def _finish(source: str, alerts: List[dict]) -> dict:
        # (1) role filter + (2) i18n decoration (NOTIF-5).
        filtered = [a for a in alerts if _role_can_see(role, a.get("kind"))]
        decorated = [_decorate(a) for a in filtered[:limit]]
        REQUESTS.labels("alerts", "ok").inc()
        return {"source": source, "alerts": decorated, "count": len(decorated), "role": role}

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
            return _finish("anomaly", alerts)
        log.info("alerts_upstream_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("alerts_upstream_unreachable", url=url, error=str(exc))

    # --- Degrade: read jnpa.alerts directly ---
    rows = await _db_alerts(state, kind=kind, limit=limit)
    return _finish("db", rows)


@router.post("/{alert_id}/ack")
async def ack_alert(alert_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Mark an alert acknowledged (NOTIF-5 ack-tracking). Writes ack=true to
    jnpa.alerts; degrades gracefully (ack recorded as best-effort) if the DB is
    unreachable so the demo never hard-fails on it."""
    from jnpa_shared.db import execute

    sql = "UPDATE jnpa.alerts SET ack = true WHERE id = :id"
    try:
        await execute(sql, {"id": alert_id}, dsn=state.cfg.postgres_dsn)
        REQUESTS.labels("alerts", "ok").inc()
        return {"id": alert_id, "ack": True, "persisted": True}
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("alert_ack_db_failed", id=alert_id, error=str(exc))
        # Best-effort: acknowledge to the caller even when the DB is down so the
        # UI can optimistically mark it; a reconciler can re-apply later.
        return {"id": alert_id, "ack": True, "persisted": False}


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
