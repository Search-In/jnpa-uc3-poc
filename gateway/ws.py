"""WebSocket fan-out hub for /api/ws.

A single hub holds every connected dashboard / PWA client. Producers (the Kafka
alert + traffic pumps, the MQTT truck-position pump, and the orchestrator's
decision emitter) call ``broadcast(type, payload)`` and the hub pushes the
message to all live clients, dropping any that have gone away.

Emitted message envelope:

    {"type": "alert"|"traffic"|"truck_position"|"decision", "payload": {...}}
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Set

from fastapi import WebSocket

from .logging import get_logger
from .metrics import WS_CLIENTS

log = get_logger("gateway.ws")


class WsHub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)
        WS_CLIENTS.set(len(self._clients))
        log.info("ws_connect", clients=len(self._clients))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
        WS_CLIENTS.set(len(self._clients))
        log.info("ws_disconnect", clients=len(self._clients))

    async def broadcast(self, type_: str, payload: Any) -> None:
        """Send ``{"type": type_, "payload": payload}`` to every live client."""
        if not self._clients:
            return
        message = {"type": type_, "payload": payload}
        async with self._lock:
            targets = list(self._clients)
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
            WS_CLIENTS.set(len(self._clients))

    @property
    def client_count(self) -> int:
        return len(self._clients)


__all__ = ["WsHub"]
