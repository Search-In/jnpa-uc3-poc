"""Notification Dispatcher — the single fan-out seam above the three transports.

    Alert / Event Engine
            |
    dispatch()  ..................  this module
            |
    ------------------------------------------------
    |                 |                    |
    WebSocket        WebPush            Firebase FCM
    (gateway/ws.py)  (routers/push.py)  (routers/push.py::deliver_fcm)

Every driver-facing advisory should go through ``dispatch()`` so all three
transports stay in lock-step and each degrades independently:

* **WebSocket** — always attempted; the live, in-app path (dashboard + a
  foregrounded PWA). Never removed.
* **WebPush** — best-effort; only when VAPID is configured and the device has a
  subscription. Unchanged behaviour.
* **FCM** — best-effort; only when Firebase is configured and the device has a
  registered token. NEW, added alongside — never replaces the others.

The client de-dupes across transports (mobile-pwa/src/lib/notify.ts keys on a
stable ``tag``), so sending over all three is safe: the driver sees one banner.

``dispatch()`` returns a per-channel :class:`DispatchResult` for evidence/tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .logging import get_logger
from .routers import push
from .state import GatewayState

log = get_logger("gateway.dispatch")


@dataclass
class DispatchResult:
    ws: bool = False
    webpush: bool = False
    fcm: bool = False

    def as_dict(self) -> Dict[str, bool]:
        return {"ws": self.ws, "webpush": self.webpush, "fcm": self.fcm}


async def dispatch(
    gw: GatewayState,
    device_id: str,
    payload: Dict[str, Any],
    *,
    ws_type: str = "alert",
    ws: bool = True,
    webpush: bool = True,
    fcm: bool = True,
) -> DispatchResult:
    """Fan one advisory out to a specific driver over every enabled transport.

    ``payload`` is the driver-facing advisory (``title``/``body``/``type``/
    ``href``/…). ``ws_type`` is the WebSocket frame type (e.g. ``reroute``,
    ``alert``). Each leg is independently guarded — one failing transport never
    blocks the others.
    """
    result = DispatchResult()

    if ws:
        try:
            await gw.ws.broadcast(ws_type, payload)
            result.ws = True
        except Exception as exc:  # noqa: BLE001
            log.warning("dispatch_ws_failed", device_id=device_id, error=str(exc))

    if webpush:
        try:
            result.webpush = await push.deliver(gw, device_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("dispatch_webpush_failed", device_id=device_id, error=str(exc))

    if fcm:
        try:
            result.fcm = await push.deliver_fcm(gw, device_id, payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("dispatch_fcm_failed", device_id=device_id, error=str(exc))

    return result


async def dispatch_alert(
    gw: GatewayState,
    device_id: Optional[str],
    *,
    kind: str,
    title: str,
    body: str,
    href: Optional[str] = None,
    category: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[DispatchResult]:
    """Convenience wrapper for the alert engine (congestion / parking / geofence /
    customs / emergency). Builds the standard advisory envelope and dispatches it
    over WS + WebPush + FCM.

    Returns None (no-op) when ``device_id`` is unknown — an alert with no bound
    device still reaches the control-room dashboard via the normal alert pump; it
    just has no driver to push to.
    """
    if not device_id:
        return None
    payload: Dict[str, Any] = {
        "type": kind,
        "title": title,
        "body": body,
        "device_id": device_id,
        "category": category or "info",
    }
    if href:
        payload["href"] = href
    if extra:
        payload.update(extra)
    return await dispatch(gw, device_id, payload, ws_type="alert")


__all__ = ["dispatch", "dispatch_alert", "DispatchResult"]
