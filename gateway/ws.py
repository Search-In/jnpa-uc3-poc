"""WebSocket fan-out hub for /api/ws.

A single hub holds every connected dashboard / PWA client. Producers (the Kafka
alert + traffic pumps, the MQTT truck-position pump, and the orchestrator's
decision emitter) call ``broadcast(type, payload)`` and the hub pushes the
message to all live clients, dropping any that have gone away.

Emitted message envelope:

    {"type": "alert"|"traffic"|"truck_position"|"decision", "payload": {...}}

Per-socket identity (notification isolation)
--------------------------------------------
Every connection carries an identity record: ``device_id`` (the driver's paired
TRK-id) and ``role`` (the JWT role, when auth is enabled). A socket that has a
``device_id`` is a **driver socket**; one that does not is a **control-room
socket** (dashboard) and keeps seeing the full operational picture exactly as
before.

Identity is set two ways, because ``AUTH_ENABLED`` is off in the demo profile
and a driver socket would otherwise be anonymous:

* from the DRIVER JWT's ``device_id`` claim at handshake (auth enabled), and
* from a ``{"cmd": "identify", "device_id": "TRK-…"}`` frame the PWA's realtime
  worker sends on open (always).

``broadcast(..., device_id=X)`` then delivers an **addressed** frame to the
control room plus the sockets bound to X only — driver B never receives driver
A's advisory. An unaddressed ``broadcast()`` behaves exactly as it always did.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket

from .logging import get_logger
from .metrics import WS_CLIENTS

log = get_logger("gateway.ws")


class WsHub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        # ws -> {"device_id": str|None, "role": str|None}. A socket with a
        # device_id is driver-scoped; one without is a control-room client.
        self._identity: Dict[WebSocket, Dict[str, Optional[str]]] = {}
        self._lock = asyncio.Lock()

    async def connect(
        self, ws: WebSocket, *, device_id: Optional[str] = None, role: Optional[str] = None
    ) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
            self._identity[ws] = {"device_id": device_id, "role": role}
        WS_CLIENTS.set(len(self._clients))
        log.info("ws_connect", clients=len(self._clients), device_id=device_id, role=role)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self._identity.pop(ws, None)
        WS_CLIENTS.set(len(self._clients))
        log.info("ws_disconnect", clients=len(self._clients))

    async def identify(
        self, ws: WebSocket, *, device_id: Optional[str] = None, role: Optional[str] = None
    ) -> None:
        """Late-bind a socket's identity (the PWA's ``identify`` frame).

        Needed when ``AUTH_ENABLED`` is off: the handshake carries no token, so
        the driver socket would otherwise be indistinguishable from a dashboard
        and would keep receiving every driver's advisory.

        A device_id already established from a verified JWT is NOT overwritten —
        a client may not re-point an authenticated socket at another device.
        """
        async with self._lock:
            ident = self._identity.get(ws)
            if ident is None:
                return
            if device_id and not (ident.get("role") and ident.get("device_id")):
                ident["device_id"] = device_id
            if role and not ident.get("role"):
                ident["role"] = role
        log.debug("ws_identify", device_id=device_id, role=role)

    def _wants(self, ws: WebSocket, device_id: Optional[str]) -> bool:
        """Should ``ws`` receive a frame addressed to ``device_id``?

        * Unaddressed frame (device_id None) -> everyone, unchanged behaviour.
        * Control-room socket (no identity)  -> everything, unchanged behaviour.
        * Driver socket                      -> only its own device's frames.

        A socket that is known to be a DRIVER but has no device_id bound fails
        CLOSED (receives no addressed frames) so an unidentified driver can
        never be leaked another driver's advisory.
        """
        if device_id is None:
            return True
        ident = self._identity.get(ws)
        if not ident:
            return True  # unknown socket -> treat as control-room (pre-existing)
        bound = ident.get("device_id")
        if bound is not None:
            return bound == device_id
        return ident.get("role") != "DRIVER"

    async def broadcast(
        self, type_: str, payload: Any, *, device_id: Optional[str] = None
    ) -> None:
        """Send ``{"type": type_, "payload": payload}`` to the relevant clients.

        With ``device_id`` set the frame is *addressed*: it reaches the control
        room and the sockets bound to that device only. Without it, every live
        client receives it (the historical behaviour every dashboard producer
        relies on).
        """
        if not self._clients:
            return
        message = {"type": type_, "payload": payload}
        async with self._lock:
            targets = [ws for ws in self._clients if self._wants(ws, device_id)]
        dead = []
        for ws in targets:
            try:
                await ws.send_json(message)
            except Exception:  # client vanished mid-send
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._identity.pop(ws, None)
            WS_CLIENTS.set(len(self._clients))

    def identity_of(self, ws: WebSocket) -> Dict[str, Optional[str]]:
        """The identity record bound to ``ws`` (empty dict when unknown)."""
        return dict(self._identity.get(ws) or {})

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def driver_count(self) -> int:
        """Sockets bound to a device (i.e. driver PWAs), for /api/notifications/health."""
        return sum(1 for i in self._identity.values() if i.get("device_id"))


__all__ = ["WsHub"]
