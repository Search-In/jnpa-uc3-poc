"""/api/notifications — notification-pipeline health + delivery-trail introspection.

Read-only surface over the notification fan-out. It does NOT duplicate the
dispatcher (``gateway/notifications.py``) or the push registration/delivery surface
(``gateway/routers/push.py``) — it only reports on them, closing the UC-3 audit's
"add a notification health endpoint" and "verify the delivery trail" items:

    GET /api/notifications/health   -> {websocket, webpush, fcm, sms} + detail
    GET /api/notifications/recent   -> recent core.notification rows (delivery trail)

The three device/WS transports and SMS each report whether they are actually
usable right now (keys present, library importable, provider configured), so the
demo can show the real posture instead of an assumed one. No delivery is faked:
statuses come straight from the durable core.notification rows the dispatcher
writes (PENDING / SENT / DELIVERED / FAILED / SKIPPED / NO_SUBSCRIPTION).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query

from .. import firebase, sms
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from . import push

log = get_logger("gateway.notifications_api")

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("/health")
async def health(state: GatewayState = Depends(get_state)) -> dict:
    """Which notification transports are live right now.

    ``websocket`` is always true (the in-process WsHub needs no configuration);
    ``webpush`` is true only when VAPID keys are set AND ``pywebpush`` is
    importable; ``fcm`` only when a Firebase credential loaded; ``sms`` only when a
    delivering provider is configured (the default no-op reports false).
    """
    cfg = state.cfg
    webpush_configured = bool(cfg.vapid_public_key.strip() and cfg.vapid_private_key.strip())
    webpush_ok = webpush_configured and push._pywebpush_available()
    fcm = firebase.status_dict(cfg)
    provider = sms.get_provider()
    # The no-op provider records intent but never delivers -> report sms:false.
    sms_ok = provider.name not in ("none", "")

    REQUESTS.labels("notifications", "ok").inc()
    return {
        # The four booleans the audit asked for.
        "websocket": True,
        "webpush": bool(webpush_ok),
        "fcm": bool(fcm.get("configured")),
        "sms": bool(sms_ok),
        # Evidence detail (never faked — reflects real config/state).
        "detail": {
            "websocket": {"clients": state.ws.client_count},
            "webpush": {
                "vapid_configured": webpush_configured,
                "pywebpush_available": push._pywebpush_available(),
                "subscriptions": len(push.SUBSCRIPTIONS),
            },
            "fcm": {**fcm, "tokens": len(push.FCM_TOKENS)},
            "sms": {"provider": provider.name, "delivers": sms_ok},
        },
    }


@router.get("/recent")
async def recent(
    limit: int = Query(default=50, ge=1, le=500),
    receiver: str | None = Query(default=None),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Recent rows from the durable core.notification delivery trail.

    Powers the notification-pipeline verification (and a demo evidence view): every
    dispatch — geofence violation, no-parking, restricted zone, reroute, customs,
    congestion — leaves a row here with its real delivery_status.
    """
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return {"records": [], "count": 0}
    from jnpa_shared.db import fetch_all

    try:
        if receiver:
            rows = await fetch_all(
                """SELECT id, event_id, channel, receiver, message, delivery_status,
                          provider_response, created_at
                   FROM core.notification WHERE receiver = :r
                   ORDER BY created_at DESC LIMIT :n""",
                {"r": receiver, "n": limit}, dsn=dsn,
            )
        else:
            rows = await fetch_all(
                """SELECT id, event_id, channel, receiver, message, delivery_status,
                          provider_response, created_at
                   FROM core.notification ORDER BY created_at DESC LIMIT :n""",
                {"n": limit}, dsn=dsn,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("notifications_recent_failed", error=str(exc))
        return {"records": [], "count": 0}

    records: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = d["created_at"].isoformat()
        records.append(d)
    REQUESTS.labels("notifications", "ok").inc()
    return {"records": records, "count": len(records)}
