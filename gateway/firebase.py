"""Firebase Admin SDK seam — FCM push delivery + Phone-Auth ID-token verify.

This is the *third* driver-notification transport, added ALONGSIDE the existing
WebSocket fan-out (``gateway/ws.py``) and WebPush/VAPID channel
(``gateway/routers/push.py``). Nothing here replaces those — the unified
dispatcher (``gateway/notifications.py``) fans an advisory out over all three and
each leg degrades independently.

Design mirrors the other optional seams in this service (``sms.py``,
``objectstore.py``): the heavy dependency (``firebase-admin``) is imported
lazily, the credential is loaded from a path *outside* the repo, and every entry
point is best-effort — a missing/blank configuration disables FCM cleanly and
the demo keeps running on WebSocket + WebPush.

Configuration (see ``gateway/config.py``):

    FIREBASE_PROJECT_ID            e.g. jnpa3-e23e8
    FIREBASE_SERVICE_ACCOUNT_PATH  absolute path to the admin-SDK JSON key
                                   (never committed; mount it as a secret)

If ``FIREBASE_SERVICE_ACCOUNT_PATH`` is unset the module also honours the
Google-standard ``GOOGLE_APPLICATION_CREDENTIALS`` and an inline
``FIREBASE_CREDENTIALS_JSON`` (the raw JSON string), so it works both in a
mounted-secret container and a bare env-var deployment.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

from .logging import get_logger

log = get_logger("gateway.firebase")

# Guarded, initialise-once state. `_app` is the firebase_admin.App; `_status`
# records why init failed (surfaced by /api/push/status for the demo).
_lock = threading.Lock()
_app: Any = None
_init_done = False
_status: str = "uninitialised"


def _load_credential(cfg) -> Optional[Any]:
    """Build a firebase_admin credential from the configured source, or None."""
    from firebase_admin import credentials  # lazy

    # 1) explicit path (preferred — a mounted secret file)
    path = (getattr(cfg, "firebase_service_account_path", "") or "").strip()
    if not path:
        path = (os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "") or "").strip()
    if path:
        if not os.path.isfile(path):
            log.warning("firebase_key_missing", path=path)
            return None
        return credentials.Certificate(path)

    # 2) inline JSON (bare env-var deployments / CI)
    raw = (os.environ.get("FIREBASE_CREDENTIALS_JSON", "") or "").strip()
    if raw:
        try:
            return credentials.Certificate(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("firebase_inline_json_invalid", error=str(exc))
            return None
    return None


def init_firebase(cfg) -> bool:
    """Initialise the Firebase Admin app exactly once. Returns True if ready.

    Idempotent and thread-safe. A missing dependency or credential is logged and
    swallowed — callers treat a False as "FCM disabled" and fall back to the
    other transports.
    """
    global _app, _init_done, _status
    if _init_done:
        return _app is not None
    with _lock:
        if _init_done:
            return _app is not None
        _init_done = True
        try:
            import firebase_admin  # lazy
        except Exception as exc:  # noqa: BLE001
            _status = "sdk_missing"
            log.warning("firebase_admin_unavailable", error=str(exc))
            return False
        cred = _load_credential(cfg)
        if cred is None:
            _status = "no_credential"
            log.info("firebase_not_configured")
            return False
        try:
            opts: Dict[str, Any] = {}
            project_id = (getattr(cfg, "firebase_project_id", "") or "").strip()
            if project_id:
                opts["projectId"] = project_id
            _app = firebase_admin.initialize_app(cred, opts or None)
            _status = "ready"
            log.info("firebase_initialised", project_id=project_id or "(from-key)")
            return True
        except Exception as exc:  # noqa: BLE001
            _status = f"init_failed:{exc}"
            log.warning("firebase_init_failed", error=str(exc))
            _app = None
            return False


def is_ready() -> bool:
    return _app is not None


def status() -> str:
    return _status


def status_dict(cfg) -> Dict[str, Any]:
    """Introspection payload for /api/push/status (demo evidence)."""
    # Trigger a lazy init so the first status call reflects real readiness.
    init_firebase(cfg)
    return {
        "configured": is_ready(),
        "status": _status,
        "project_id": (getattr(cfg, "firebase_project_id", "") or "") or None,
    }


# --------------------------------------------------------------------- FCM send
def send_push_notification(
    *,
    token: str,
    title: str,
    body: str,
    notification_type: str = "advisory",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Send one FCM message to a device token. Best-effort; never raises.

    Sends a **data-only** message (all values stringified) so the PWA service
    worker owns rendering — this keeps a single, consistently-styled
    notification and avoids the browser auto-displaying a second one. The SW's
    existing ``push`` handler reads ``title``/``body``/``href`` straight out of
    the data map (it normalises the FCM envelope).

    Returns ``{sent, id?, error?, reason?}``.
    """
    if not is_ready():
        return {"sent": False, "reason": _status or "not_ready"}
    if not token:
        return {"sent": False, "reason": "no_token"}
    try:
        from firebase_admin import messaging  # lazy

        payload: Dict[str, str] = {
            "type": str(notification_type),
            "title": str(title),
            "body": str(body),
        }
        for k, v in (data or {}).items():
            if v is None:
                continue
            payload[str(k)] = v if isinstance(v, str) else json.dumps(v)

        message = messaging.Message(
            data=payload,
            token=token,
            android=messaging.AndroidConfig(priority="high"),
            webpush=messaging.WebpushConfig(headers={"Urgency": "high", "TTL": "120"}),
        )
        msg_id = messaging.send(message)
        return {"sent": True, "id": msg_id}
    except Exception as exc:  # noqa: BLE001
        # A messaging.UnregisteredError means the token is dead — surface the
        # class so the caller can prune it from storage.
        name = type(exc).__name__
        log.warning("fcm_send_failed", error=str(exc), kind=name)
        return {"sent": False, "error": str(exc), "kind": name}


def token_is_dead(send_result: Dict[str, Any]) -> bool:
    """True when an FCM send failed because the token is no longer valid."""
    kind = (send_result or {}).get("kind", "")
    return kind in {"UnregisteredError", "SenderIdMismatchError"} or "not-registered" in str(
        (send_result or {}).get("error", "")
    ).lower()


# ---------------------------------------------------------------- Phone Auth
def verify_id_token(cfg, id_token: str) -> Optional[Dict[str, Any]]:
    """Verify a Firebase ID token (from client Phone Auth). Returns the decoded
    claims (``uid``, ``phone_number``, …) or None on any failure.

    Used by the Firebase-Phone login endpoint to authenticate the driver before
    minting the platform's own DRIVER JWT — Firebase handles the OTP round-trip,
    we keep the existing session/device-binding model unchanged.
    """
    if not init_firebase(cfg):
        return None
    try:
        from firebase_admin import auth  # lazy

        return auth.verify_id_token(id_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("firebase_verify_failed", error=str(exc))
        return None


__all__ = [
    "init_firebase",
    "is_ready",
    "status",
    "status_dict",
    "send_push_notification",
    "token_is_dead",
    "verify_id_token",
]
