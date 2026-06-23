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

from ..auth import auth_enabled, principal_from_token
from ..logging import get_logger
from ..state import GatewayState

log = get_logger("gateway.ws_router")

router = APIRouter(tags=["ws"])

# WebSocket close code for a policy violation (RFC 6455).
_WS_POLICY_VIOLATION = 1008


@router.websocket("/api/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    state: GatewayState = ws.app.state.gw

    # Auth: the HTTP middleware skips /api/ws (it can't read the body of an
    # upgrade), so validate the bearer here. The browser can't set an
    # Authorization header on a WS handshake, so the token rides as ?token=.
    # When AUTH_ENABLED is off this is a no-op and the socket stays open.
    if auth_enabled():
        token = ws.query_params.get("token", "")
        try:
            principal_from_token(token)
        except ValueError as exc:
            log.info("ws_auth_rejected", error=str(exc))
            await ws.close(code=_WS_POLICY_VIOLATION)
            return

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
