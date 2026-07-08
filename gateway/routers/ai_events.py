"""/api/ai — unified AI-event ingestion + persistence (Phase 2 · Track 3).

A single sink every AI output funnels through (ANPR, vehicle detection, illegal
parking, wrong direction, queue detection, traffic density). Each event is:

  1. persisted to jnpa.digital_twin_events   (via audit.record_event — reused)
  2. raised as an alert in jnpa.alerts        (when severity warrants)
  3. logged as a driver notification          (via audit.log_notification — reused)

Reuses the audit-framework helpers by CALLING them (the framework code is not
modified). ANPR reads additionally persist to jnpa.anpr_reads via the gateway's
ANPR pump (already wired) — this endpoint is the operational-action funnel.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import audit
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.ai_events")

router = APIRouter(prefix="/api/ai", tags=["ai-events"])

_ALERT_NS = uuid.UUID("9a8b7c6d-5e4f-3a2b-1c0d-9e8f7a6b5c4d")

# AI event types that should raise an alert + notify by default.
_ALERTING = {
    "ILLEGAL_PARKING", "WRONG_DIRECTION", "NO_PARKING_VIOLATION",
    "QUEUE_OVERFLOW", "CONGESTION_ALERT", "OVERSPEED",
}


@router.post("/event")
async def ingest_ai_event(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Ingest one AI event. Body: {event_type, vehicle_id?, driver_id?, location?,
    payload?, severity?, alert?, notify?}. Persists to digital_twin_events and,
    when warranted, raises an alert + notification."""
    event_type = (body.get("event_type") or "").strip().upper()
    if not event_type:
        raise HTTPException(status_code=422, detail={"error": "event_type_required"})
    vehicle_id = body.get("vehicle_id")
    driver_id = body.get("driver_id")
    location = body.get("location") or {}
    payload = body.get("payload") or {}
    severity = (body.get("severity") or "info").lower()
    should_alert = bool(body.get("alert", event_type in _ALERTING))
    should_notify = bool(body.get("notify", severity in ("warning", "critical")))

    # 1) unified event timeline (reuse framework writer)
    await audit.record_event(
        event_type=event_type, vehicle_id=vehicle_id, driver_id=driver_id,
        location=location, payload=payload, dsn=state.cfg.postgres_dsn,
    )

    alert_id = None
    if should_alert:
        alert_id = str(uuid.uuid5(_ALERT_NS, f"{event_type}|{vehicle_id}|{location}"))
        from jnpa_shared.db import execute
        import json

        try:
            await execute(
                """
                INSERT INTO jnpa.alerts (id, kind, severity, plate, payload)
                VALUES (CAST(:id AS uuid), :kind, :sev, :plate, CAST(:p AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """,
                {"id": alert_id, "kind": event_type,
                 "sev": "critical" if severity == "critical" else "warning",
                 "plate": vehicle_id,
                 "p": json.dumps({"source": "ai-event", "driver_id": driver_id,
                                  **location, **payload}, default=str)},
                dsn=state.cfg.postgres_dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ai_alert_write_failed", error=str(exc))

    if should_notify:
        await audit.log_notification(
            channel="push", event_id=alert_id, receiver=driver_id or vehicle_id,
            message=f"AI alert: {event_type}", delivery_status="SENT",
            provider_response={"event_type": event_type, "severity": severity},
            dsn=state.cfg.postgres_dsn,
        )

    REQUESTS.labels("ai_event", "ok").inc()
    return {"ingested": True, "event_type": event_type, "alert_id": alert_id,
            "alerted": should_alert, "notified": should_notify}


@router.get("/events")
async def list_ai_events(
    event_type: str | None = None,
    limit: int = 100,
    state: GatewayState = Depends(get_state),
) -> dict:
    """Recent AI events from jnpa.digital_twin_events (RDS)."""
    from jnpa_shared.db import fetch_all

    where = ""
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
    if event_type:
        where = "WHERE event_type = :et"
        params["et"] = event_type.upper()
    try:
        rows = await fetch_all(
            f"""
            SELECT id, event_type, vehicle_id, driver_id, location, payload, created_at
            FROM jnpa.digital_twin_events {where}
            ORDER BY created_at DESC LIMIT :limit
            """,
            params, dsn=state.cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        return {"events": [], "count": 0}
    out = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        out.append(d)
    REQUESTS.labels("ai_event", "ok").inc()
    return {"events": out, "count": len(out)}


__all__ = ["router"]
