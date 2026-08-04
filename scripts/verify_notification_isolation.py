#!/usr/bin/env python3
"""Verify driver-notification isolation against a RUNNING gateway (EC2 / local).

Proves the fix for the "notifications reach all users" defect end-to-end over a
real network: two driver sockets are opened, three advisories are dispatched
(A, B, A), and each socket is checked to have received ONLY its own.

    python3 scripts/verify_notification_isolation.py --url http://localhost:8000
    python3 scripts/verify_notification_isolation.py --url https://<ec2-host> \
        --device-a TRK-000001 --device-b TRK-000002 --token "$DRIVER_JWT"

Exit code 0 = isolated (PASS), 1 = leaking (FAIL). Nothing is written to the DB
beyond the normal digital-twin event the /api/ai/event path always records, and
no business state is mutated.

Requires: websockets, httpx (both already in the gateway image).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

try:
    import httpx
    import websockets
except ImportError as exc:  # pragma: no cover
    sys.exit(f"missing dependency: {exc}. pip install websockets httpx")

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
RECV_TIMEOUT_S = 8.0


def _ws_url(base: str, device: str | None, token: str | None) -> str:
    url = base.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    params = []
    if token:
        params.append(f"token={token}")
    if device:
        params.append(f"device={device}")
    return f"{url}/api/ws" + ("?" + "&".join(params) if params else "")


async def _drain_hello(ws) -> None:
    """Consume the server's hello frame so later reads see only advisories."""
    frame = json.loads(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S))
    if frame.get("type") != "hello":
        raise AssertionError(f"expected hello, got {frame.get('type')}")


async def _recv_alert(ws, label: str):
    """Next `alert` frame, or None if the socket stays quiet (the good case)."""
    try:
        while True:
            frame = json.loads(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT_S))
            if frame.get("type") == "alert":
                return frame["payload"]
    except asyncio.TimeoutError:
        print(f"  {label}: silent for {RECV_TIMEOUT_S:g}s")
        return None


async def _fire(client: httpx.AsyncClient, base: str, device: str, marker: str,
                token: str | None) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await client.post(
        f"{base.rstrip('/')}/api/ai/event",
        json={"event_type": "WRONG_DIRECTION", "device_id": device,
              "payload": {"message": marker}, "severity": "critical"},
        headers=headers, timeout=20.0,
    )
    r.raise_for_status()
    disp = r.json().get("dispatched")
    print(f"  dispatched {marker} -> {device}  (transports: {disp})")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000", help="gateway base URL")
    ap.add_argument("--device-a", default="TRK-000001")
    ap.add_argument("--device-b", default="TRK-000002")
    ap.add_argument("--token", default=None, help="DRIVER/admin JWT if AUTH_ENABLED=true")
    args = ap.parse_args()

    print(f"\nNotification isolation check against {args.url}")
    print(f"  driver A = {args.device_a}\n  driver B = {args.device_b}\n")

    ws_a = await websockets.connect(_ws_url(args.url, args.device_a, args.token))
    ws_b = await websockets.connect(_ws_url(args.url, args.device_b, args.token))
    dash = await websockets.connect(_ws_url(args.url, None, args.token))
    try:
        for ws in (ws_a, ws_b, dash):
            await _drain_hello(ws)
        print("  3 sockets open (driver A, driver B, control room)\n")

        async with httpx.AsyncClient() as client:
            await _fire(client, args.url, args.device_a, "ISO-A-1", args.token)
            await _fire(client, args.url, args.device_b, "ISO-B-1", args.token)
            await _fire(client, args.url, args.device_a, "ISO-A-2", args.token)

        print("\nreading sockets…")
        a1 = await _recv_alert(ws_a, "driver A")
        a2 = await _recv_alert(ws_a, "driver A")
        b1 = await _recv_alert(ws_b, "driver B")
        d = [await _recv_alert(dash, "control room") for _ in range(3)]
    finally:
        for ws in (ws_a, ws_b, dash):
            await ws.close()

    a_bodies = [f.get("body") if f else None for f in (a1, a2)]
    b_bodies = [f.get("body") if f else None for f in (b1,)]
    d_bodies = [f.get("body") if f else None for f in d]

    print(f"\n  driver A received : {a_bodies}")
    print(f"  driver B received : {b_bodies}")
    print(f"  control room      : {d_bodies}\n")

    checks = [
        ("driver A receives its own advisories", a_bodies == ["ISO-A-1", "ISO-A-2"]),
        ("driver B does NOT receive A's advisory", "ISO-A-1" not in b_bodies
                                                   and "ISO-A-2" not in b_bodies),
        ("driver B receives its own advisory", b_bodies == ["ISO-B-1"]),
        ("driver A does NOT receive B's advisory", "ISO-B-1" not in a_bodies),
        ("advisories are addressed", (a1 or {}).get("device_id") == args.device_a),
        ("control room still sees everything",
         sorted(x for x in d_bodies if x) == ["ISO-A-1", "ISO-A-2", "ISO-B-1"]),
    ]
    ok = True
    for label, passed in checks:
        print(f"  {GREEN}PASS{RESET}  {label}" if passed else f"  {RED}FAIL{RESET}  {label}")
        ok = ok and passed

    if ok:
        print(f"\n{GREEN}ISOLATION VERIFIED — each driver received only their own notification.{RESET}\n")
        return 0
    print(f"\n{RED}ISOLATION BROKEN — notifications are crossing between drivers.{RESET}")
    print(f"{YELLOW}Check: gateway rebuilt after the fix? PWA rebuilt and cache cleared?{RESET}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
