"""/api/ws — WebSocket fan-out endpoint.

A dashboard / PWA opens one socket and receives every platform event:

    {"type": "alert",          "payload": Alert}
    {"type": "traffic",        "payload": Snapshot}
    {"type": "truck_position", "payload": TruckTelemetry}   (sampled 1-in-50)
    {"type": "decision",       "payload": DecisionPath}      (only on fallback)

The server pushes; it ignores anything the client sends (the socket is a
one-way feed) but reads to detect disconnects. On connect we send a small
``hello`` frame so the client can confirm the stream is live.
"""
from __future__ import annotations

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..logging import get_logger
from ..state import GatewayState

log = get_logger("gateway.ws_router")

router = APIRouter(tags=["ws"])


@router.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    state: GatewayState = ws.app.state.gw
    await state.ws.connect(ws)
    try:
        await ws.send_json({"type": "hello", "payload": {"service": "jnpa-gateway",
                                                         "channels": ["alert", "traffic",
                                                                      "truck_position", "decision"]}})
        while True:
            # We don't act on inbound messages, but receiving lets us notice the
            # client going away promptly (raises WebSocketDisconnect).
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("ws_error", error=str(exc))
    finally:
        await state.ws.disconnect(ws)
