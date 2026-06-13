"""/api/ulip/proxy — ULIP (Unified Logistics Interface Platform) relay proxy.

This is the SECONDARY GPS source for the trucking-app fallback chain. When a key
is configured (``ULIP_API_KEY`` + ``GATEWAY_ULIP_URL``) it proxies to the real
ULIP relay; otherwise it returns a deterministic *mock* relay position so the
SECONDARY rung is demonstrable offline (spec: "mock if no key").
"""
from __future__ import annotations

import hashlib

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ..logging import get_logger
from ..state import GatewayState, get_state

log = get_logger("gateway.ulip")

router = APIRouter(prefix="/api/ulip", tags=["ulip"])

# NH-348 corridor bounding box (JNPA -> Karal Phata) for plausible mock points.
_LAT_LO, _LAT_HI = 18.78, 18.95
_LON_LO, _LON_HI = 72.95, 73.08


def _mock_relay(device_id: str) -> dict:
    """Deterministic mock ULIP relay GPS (no RNG, reproducible for demos)."""
    h = int.from_bytes(hashlib.sha256(device_id.encode()).digest()[:8], "big")
    lat = round(_LAT_LO + (h % 1000) / 1000.0 * (_LAT_HI - _LAT_LO), 6)
    lon = round(_LON_LO + ((h >> 10) % 1000) / 1000.0 * (_LON_HI - _LON_LO), 6)
    return {
        "device_id": device_id,
        "source": "ulip-mock",
        "lat": lat,
        "lon": lon,
        "speed_kmh": 30.0 + (h % 25),
        "heading": h % 360,
        "provider": "ULIP",
        "mock": True,
    }


@router.get("/proxy/{device_id}")
async def ulip_proxy(device_id: str, state: GatewayState = Depends(get_state)) -> dict:
    cfg = state.cfg
    # Live ULIP relay only when both a base URL and a key are configured.
    if cfg.ulip_url and cfg.ulip_api_key:
        url = cfg.ulip_url.rstrip("/") + f"/gps/{device_id}"
        try:
            resp = await state.http.get(
                url, headers={"Authorization": f"Bearer {cfg.ulip_api_key}"}
            )
        except httpx.HTTPError as exc:
            log.warning("ulip_relay_unreachable", url=url, error=str(exc))
            raise HTTPException(status_code=502, detail={"error": "ulip_unreachable"})
        if resp.status_code == 200:
            data = resp.json()
            data.setdefault("source", "ulip-live")
            data["mock"] = False
            return data
        if resp.status_code == 404:
            raise HTTPException(status_code=404, detail={"error": "not_found", "device_id": device_id})
        raise HTTPException(status_code=502, detail={"error": "ulip_error", "status": resp.status_code})

    # No key configured -> mock relay (spec).
    return _mock_relay(device_id)
