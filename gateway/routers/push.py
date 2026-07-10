"""/api/push — driver push registration + delivery for the trucking-app PWA.

The PWA registers here so the gateway can reach a backgrounded / closed app over
two independent push transports:

    GET  /api/push/vapid-public-key    -> the VAPID public key the SW subscribes with
    POST /api/push/subscribe           -> store a WebPush subscription for a device
    POST /api/push/register-device     -> store a Firebase FCM device token
    POST /api/push/unsubscribe         -> drop a device's registrations
    GET  /api/push/status              -> demo introspection (transports configured?)
    POST /api/push/test/{device_id}    -> send a test notification (demo / e2e)

Two transports, added over time, both best-effort and independently degrading:

* **WebPush / VAPID** — keys from ``VAPID_PUBLIC_KEY`` / ``VAPID_PRIVATE_KEY``
  (``make vapid-keys``). Delivered with ``pywebpush`` (imported lazily).
* **Firebase FCM** — service account from ``FIREBASE_SERVICE_ACCOUNT_PATH``
  (``gateway/firebase.py``). Delivered with ``firebase-admin`` (imported lazily).

Registrations are persisted to ``jnpa.push_subscriptions`` (durable across
restarts — the old in-memory-only behaviour was a demo-scale shortcut). A small
in-memory write-through cache keeps the hot path fast; a cold gateway reloads a
device's registration from the DB on first delivery.

Neither transport is required: with neither configured the PWA still gets its
re-route over the WebSocket ``reroute`` frame / in-app polling fallback, so the
demo never hard-depends on a key being present.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from .. import audit, firebase
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state

log = get_logger("gateway.push")

router = APIRouter(prefix="/api/push", tags=["push"])

# In-memory write-through caches (device_id -> value). The durable copy lives in
# jnpa.push_subscriptions; these just avoid a DB round-trip on the hot path.
SUBSCRIPTIONS: Dict[str, dict] = {}   # WebPush PushSubscription dicts
FCM_TOKENS: Dict[str, str] = {}       # Firebase device tokens

_DDL = (
    """CREATE TABLE IF NOT EXISTS jnpa.push_subscriptions (
        device_id text PRIMARY KEY, driver_id text, vehicle_id text,
        webpush jsonb, fcm_token text, platform text NOT NULL DEFAULT 'web',
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_push_subs_fcm ON jnpa.push_subscriptions (fcm_token) WHERE fcm_token IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_push_subs_driver ON jnpa.push_subscriptions (driver_id)",
)
_READY: Dict[str, bool] = {}


async def _ensure(dsn: Optional[str]) -> None:
    """Idempotently provision the durable registration table (otp.py pattern)."""
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in _DDL:
        try:
            await execute(stmt, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("push_ddl_skipped", error=str(exc))
    _READY[dsn] = True


async def _upsert(dsn: Optional[str], device_id: str, **cols: Any) -> None:
    """Upsert one registration column-set, leaving unspecified columns intact."""
    if not dsn:
        return
    await _ensure(dsn)
    from jnpa_shared.db import execute

    fields = {"device_id": device_id, **cols}
    set_cols = [k for k in cols]
    insert_cols = ", ".join(fields.keys())
    insert_vals = ", ".join(f":{k}" for k in fields.keys())
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in set_cols) + ", updated_at = now()"
    # webpush is jsonb — cast its bind param.
    insert_vals = insert_vals.replace(":webpush", "CAST(:webpush AS jsonb)")
    try:
        await execute(
            f"""INSERT INTO jnpa.push_subscriptions ({insert_cols})
                VALUES ({insert_vals})
                ON CONFLICT (device_id) DO UPDATE SET {updates}""",
            fields, dsn=dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("push_upsert_failed", device_id=device_id, error=str(exc))


async def _load_webpush(state: GatewayState, device_id: str) -> Optional[dict]:
    if device_id in SUBSCRIPTIONS:
        return SUBSCRIPTIONS[device_id]
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return None
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        "SELECT webpush FROM jnpa.push_subscriptions WHERE device_id = :d",
        {"d": device_id}, dsn=dsn,
    )
    sub = row["webpush"] if row and row["webpush"] else None
    if isinstance(sub, str):
        try:
            sub = json.loads(sub)
        except Exception:  # noqa: BLE001
            sub = None
    if sub:
        SUBSCRIPTIONS[device_id] = sub
    return sub


async def resolve_device(
    state: GatewayState, *, driver_id: Optional[str] = None, vehicle_id: Optional[str] = None,
) -> Optional[str]:
    """Best-effort reverse lookup: the registered device_id for a driver/vehicle.

    Used by the alert engine (e.g. geofence) to find the PWA device to push to
    from a violation that only knows the driver_id / plate. Returns None when no
    device has registered against either key — the caller then no-ops (the alert
    still reaches the control-room dashboard over the normal WS alert frame).
    """
    dsn = state.cfg.postgres_dsn
    if not dsn or not (driver_id or vehicle_id):
        return None
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        """SELECT device_id FROM jnpa.push_subscriptions
           WHERE (:drv IS NOT NULL AND driver_id = :drv)
              OR (:veh IS NOT NULL AND vehicle_id = :veh)
           ORDER BY updated_at DESC LIMIT 1""",
        {"drv": driver_id, "veh": vehicle_id}, dsn=dsn,
    )
    return row["device_id"] if row else None


async def _load_fcm(state: GatewayState, device_id: str) -> Optional[str]:
    if device_id in FCM_TOKENS:
        return FCM_TOKENS[device_id]
    dsn = state.cfg.postgres_dsn
    if not dsn:
        return None
    from jnpa_shared.db import fetch_one

    row = await fetch_one(
        "SELECT fcm_token FROM jnpa.push_subscriptions WHERE device_id = :d",
        {"d": device_id}, dsn=dsn,
    )
    tok = row["fcm_token"] if row else None
    if tok:
        FCM_TOKENS[device_id] = tok
    return tok


@router.get("/vapid-public-key")
async def vapid_public_key(state: GatewayState = Depends(get_state)) -> dict:
    """The VAPID public key the service-worker uses for ``pushManager.subscribe``.

    Returns ``{"key": null}`` when no key is configured so the PWA can detect the
    "push disabled" state and quietly fall back to WS / polling.
    """
    key = state.cfg.vapid_public_key.strip() or None
    return {"key": key, "configured": bool(key)}


@router.post("/subscribe")
async def subscribe(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Store a PWA's WebPush subscription against its device_id.

    Body: ``{"device_id": "TRK-...", "subscription": {endpoint, keys}, driver_id?, vehicle_id?}``.
    """
    device_id = body.get("device_id")
    subscription = body.get("subscription")
    if not device_id or not isinstance(subscription, dict):
        REQUESTS.labels("push", "invalid").inc()
        raise HTTPException(status_code=422, detail={"error": "device_id_and_subscription_required"})
    SUBSCRIPTIONS[device_id] = subscription
    await _upsert(
        state.cfg.postgres_dsn, device_id,
        webpush=json.dumps(subscription),
        driver_id=body.get("driver_id"), vehicle_id=body.get("vehicle_id"),
    )
    log.info("push_subscribed", device_id=device_id, subs=len(SUBSCRIPTIONS))
    REQUESTS.labels("push", "ok").inc()
    return {"subscribed": True, "device_id": device_id, "total": len(SUBSCRIPTIONS)}


@router.post("/register-device")
async def register_device(
    body: Dict[str, Any] = Body(...),
    state: GatewayState = Depends(get_state),
) -> dict:
    """Register a Firebase FCM device token for a device.

    Body: ``{"device_id": "TRK-...", "fcm_token": "...", platform?, driver_id?, vehicle_id?}``.
    The token is stored alongside any existing WebPush subscription for the same
    device, so the dispatcher can reach the driver over either transport.
    """
    device_id = str(body.get("device_id") or "").strip()
    fcm_token = str(body.get("fcm_token") or body.get("token") or "").strip()
    if not device_id or not fcm_token:
        REQUESTS.labels("push", "invalid").inc()
        raise HTTPException(status_code=422, detail={"error": "device_id_and_fcm_token_required"})
    FCM_TOKENS[device_id] = fcm_token
    await _upsert(
        state.cfg.postgres_dsn, device_id,
        fcm_token=fcm_token, platform=str(body.get("platform") or "web"),
        driver_id=body.get("driver_id"), vehicle_id=body.get("vehicle_id"),
    )
    audit.spawn(audit.log_notification(
        channel="fcm", receiver=device_id, message="device token registered",
        delivery_status="SENT", provider_response={"platform": body.get("platform") or "web"},
    ))
    log.info("fcm_registered", device_id=device_id, tokens=len(FCM_TOKENS))
    REQUESTS.labels("push", "ok").inc()
    return {"registered": True, "device_id": device_id, "transport": "fcm"}


@router.post("/unsubscribe")
async def unsubscribe(body: Dict[str, Any] = Body(...), state: GatewayState = Depends(get_state)) -> dict:
    device_id = body.get("device_id")
    existed = SUBSCRIPTIONS.pop(device_id, None) is not None
    FCM_TOKENS.pop(device_id, None)
    dsn = state.cfg.postgres_dsn
    if dsn:
        from jnpa_shared.db import execute

        try:
            await execute("DELETE FROM jnpa.push_subscriptions WHERE device_id = :d",
                          {"d": device_id}, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("push_unsub_db_skipped", error=str(exc))
    return {"unsubscribed": existed, "device_id": device_id, "total": len(SUBSCRIPTIONS)}


@router.get("/status")
async def status(state: GatewayState = Depends(get_state)) -> dict:
    """Demo introspection: which transports are configured, and how many devices."""
    return {
        "webpush": {
            "configured": bool(state.cfg.vapid_public_key.strip() and state.cfg.vapid_private_key.strip()),
            "pywebpush_available": _pywebpush_available(),
            "subscriptions": len(SUBSCRIPTIONS),
        },
        "fcm": {
            **firebase.status_dict(state.cfg),
            "tokens": len(FCM_TOKENS),
        },
        "devices": sorted(set(SUBSCRIPTIONS) | set(FCM_TOKENS)),
    }


@router.post("/test/{device_id}")
async def test_push(device_id: str, state: GatewayState = Depends(get_state)) -> dict:
    """Send a test notification to one device over every configured transport."""
    payload = {
        "type": "test",
        "title": "JNPA Trucking",
        "body": "Push channel is live.",
        "device_id": device_id,
        "href": "#/inbox",
    }
    webpush_ok = await deliver(state, device_id, payload)
    fcm_ok = await deliver_fcm(state, device_id, payload)
    return {"device_id": device_id, "webpush": webpush_ok, "fcm": fcm_ok}


# --------------------------------------------------------------------- delivery
def _pywebpush_available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


async def deliver(state: GatewayState, device_id: str, payload: dict) -> bool:
    """Best-effort WebPush to one device. Returns True if a push was sent.

    Runs the (blocking) pywebpush call in a thread so it never stalls the event
    loop. Any failure (no sub, no key, lib missing, dead endpoint) is swallowed
    and returns False — the caller still has the WS reroute frame as a fallback.
    """
    cfg = state.cfg
    msg = payload.get("body") or payload.get("title") or payload.get("type") or ""
    event_id = payload.get("alert_id") or payload.get("case_id") or payload.get("id")

    def _log(status: str, provider: Any) -> None:
        audit.spawn(
            audit.log_notification(
                channel="webpush", event_id=event_id, receiver=device_id,
                message=str(msg), delivery_status=status, provider_response=provider,
            )
        )

    sub = await _load_webpush(state, device_id)
    if sub is None:
        log.debug("push_no_subscription", device_id=device_id)
        _log("NO_SUBSCRIPTION", {"reason": "no_subscription"})
        return False
    if not (cfg.vapid_private_key.strip() and cfg.vapid_public_key.strip()):
        log.debug("push_not_configured", device_id=device_id)
        _log("SKIPPED", {"reason": "vapid_not_configured"})
        return False
    try:
        ok = await asyncio.to_thread(_send_blocking, sub, payload, cfg)
        _log("SENT" if ok else "FAILED", {"pywebpush": ok})
        return ok
    except Exception as exc:  # noqa: BLE001
        log.warning("push_delivery_failed", device_id=device_id, error=str(exc))
        _log("FAILED", {"error": str(exc)})
        # A 404/410 means the endpoint is gone — drop the stale subscription.
        if "410" in str(exc) or "404" in str(exc):
            SUBSCRIPTIONS.pop(device_id, None)
            await _upsert(cfg.postgres_dsn, device_id, webpush=None)
        return False


async def deliver_fcm(state: GatewayState, device_id: str, payload: dict) -> bool:
    """Best-effort Firebase FCM to one device. Returns True if a message was sent.

    Runs the (blocking) firebase-admin send in a thread. A missing token / no
    Firebase config / dead token is swallowed and returns False — WebPush + WS
    remain as fallbacks. A dead token is pruned so it is not retried.
    """
    cfg = state.cfg
    token = await _load_fcm(state, device_id)
    msg = payload.get("body") or payload.get("title") or payload.get("type") or ""
    event_id = payload.get("alert_id") or payload.get("case_id") or payload.get("id")

    def _log(status: str, provider: Any) -> None:
        audit.spawn(audit.log_notification(
            channel="fcm", event_id=event_id, receiver=device_id,
            message=str(msg), delivery_status=status, provider_response=provider,
        ))

    if not token:
        _log("NO_SUBSCRIPTION", {"reason": "no_fcm_token"})
        return False
    if not firebase.init_firebase(cfg):
        _log("SKIPPED", {"reason": firebase.status()})
        return False

    # Data-only payload (SW renders it); coerce nested values to strings there.
    data = {k: v for k, v in payload.items() if k not in ("title", "body", "type")}
    result = await asyncio.to_thread(
        firebase.send_push_notification,
        token=token,
        title=str(payload.get("title") or "JNPA Trucking"),
        body=str(payload.get("body") or ""),
        notification_type=str(payload.get("type") or "advisory"),
        data=data,
    )
    if result.get("sent"):
        _log("SENT", result)
        return True
    _log("FAILED", result)
    if firebase.token_is_dead(result):
        FCM_TOKENS.pop(device_id, None)
        await _upsert(cfg.postgres_dsn, device_id, fcm_token=None)
    return False


def _send_blocking(sub: dict, payload: dict, cfg) -> bool:
    from pywebpush import webpush

    webpush(
        subscription_info=sub,
        data=json.dumps(payload),
        vapid_private_key=cfg.vapid_private_key.strip(),
        vapid_claims={"sub": cfg.vapid_subject},
        ttl=120,
    )
    return True


__all__ = ["router", "deliver", "deliver_fcm", "SUBSCRIPTIONS", "FCM_TOKENS"]
