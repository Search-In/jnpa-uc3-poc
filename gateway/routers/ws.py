"""/api/ws — WebSocket fan-out endpoint.

A dashboard / PWA opens one socket and receives every platform event:

    {"type": "alert",          "payload": Alert}
    {"type": "traffic",        "payload": Snapshot}
    {"type": "truck_position", "payload": TruckTelemetry}   (sampled 1-in-50)
    {"type": "decision",       "payload": DecisionPath}      (only on fallback)

The server pushes; the only inbound message it acts on is the ``identify``
command (below) — everything else is ignored, but reading lets it notice a
disconnect. On connect we send a small ``hello`` frame so the client can confirm
the stream is live.

Socket identity (notification isolation)
----------------------------------------
A driver socket must be distinguishable from a dashboard socket, or the hub
cannot deliver an advisory to one driver without leaking it to the others. The
device binding is established from, in order of precedence:

1. the DRIVER JWT's ``device_id`` claim (``?token=``) — verified, wins outright;
2. the ``?device=TRK-…`` handshake query param — bound immediately, so there is
   no window between connect and the first addressed frame;
3. a ``{"cmd": "identify", "device_id": "TRK-…"}`` text frame — belt-and-braces
   for a client that connected before it knew its device.

2 and 3 are unverified and only apply when the socket has no authenticated
identity, which is the demo profile (``AUTH_ENABLED=false``). They can only ever
NARROW what a socket receives, never widen it.
"""
from __future__ import annotations

import json

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
    device_id = None
    role = None
    if auth_enabled():
        token = ws.query_params.get("token", "")
        try:
            principal = principal_from_token(token)
        except ValueError as exc:
            log.info("ws_auth_rejected", error=str(exc))
            await ws.close(code=_WS_POLICY_VIOLATION)
            return
        device_id, role = principal.device_id, principal.role

    # Unauthenticated fallback binding (demo profile): the PWA appends its paired
    # device id to the socket URL. Never overrides a verified JWT binding.
    if not device_id:
        q = (ws.query_params.get("device") or "").strip()
        device_id = q or None

    await state.ws.connect(ws, device_id=device_id, role=role)
    try:
        await ws.send_json({"type": "hello", "payload": {"service": "jnpa-gateway",
                                                         "channels": ["alert", "traffic",
                                                                      "truck_position", "decision"],
                                                         "device_id": device_id}})
        while True:
            # The socket is a one-way feed apart from `identify`; receiving also
            # lets us notice the client going away (raises WebSocketDisconnect).
            raw = await ws.receive_text()
            if not raw or not raw.lstrip().startswith("{"):
                continue  # keep-alive "ping" and friends
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get("cmd") == "identify":
                dev = msg.get("device_id")
                await state.ws.identify(ws, device_id=str(dev) if dev else None)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.debug("ws_error", error=str(exc))
    finally:
        await state.ws.disconnect(ws)
