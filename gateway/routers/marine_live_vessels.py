"""/api/marine/vessels/live — Live AIS vessel positions via MarineTraffic tile scrape.

Stateless proxy: fetches from MarineTraffic's internal tile API for the JNPA
port area (Mumbai, Lat=18.927, Lon=72.895, Zoom=12 → center tile X=1438, Y=913)
and transforms the raw AIS fields into the canonical LiveVesselDTO schema.

NO data is persisted — this endpoint is pure pass-through.

    GET /api/marine/vessels/live   → list of live AIS vessel positions
"""
from __future__ import annotations

import asyncio
import time
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/api/marine/vessels", tags=["marine"])

# ---------------------------------------------------------------------------
# JNPA area tile constants (Mumbai port, Zoom 12)
# Lat=18.927, Lon=72.895  →  X=1438, Y=913
# ---------------------------------------------------------------------------
_ZOOM = 12
_CENTER_X = 1438
_CENTER_Y = 913
_TILE_URL = "https://www.marinetraffic.com/getData/get_data_json_4/z:{z}/X:{x}/Y:{y}/station:0"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JNPA-UC3-POC)",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.marinetraffic.com/",
}

# Cache for 60 seconds to avoid hammering MarineTraffic
_cache_ts: float = 0.0
_cache_data: List[dict] = []
_CACHE_TTL = 60.0


# ---------------------------------------------------------------------------
# Ship type mapping (AIS ITU-R M.1371, groups of 10)
# ---------------------------------------------------------------------------
_SHIP_TYPE_MAP: dict[int, str] = {
    0: "Unknown",
    6: "Passenger",
    7: "Passenger (HSC)",
    8: "Cargo",
    9: "Cargo (HSC)",
    10: "Tanker",
    11: "Tanker",
    12: "Tanker",
    13: "Military",
    14: "SAR",
    15: "Tug",
    16: "Port tender",
    17: "Anti-pollution",
    18: "Law enforcement",
    19: "Local vessel",
}


def _ship_type_label(code: int) -> str:
    bucket = code // 10 if code >= 10 else code
    return _SHIP_TYPE_MAP.get(bucket, "Other")


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------
class LiveVesselDTO(BaseModel):
    mmsi: str
    vessel_name: str
    imo_no: Optional[str] = None
    lat: float
    lon: float
    speed_knots: float
    course: int
    heading: Optional[int] = None
    ship_type_code: int
    ship_type_label: str
    destination: Optional[str] = None
    flag: Optional[str] = None
    length: Optional[int] = None
    elapsed_seconds: Optional[int] = None


def _transform(raw: dict) -> LiveVesselDTO:
    """Transform a raw MarineTraffic row into a LiveVesselDTO."""
    speed_raw = raw.get("SPEED") or 0
    lat_raw = raw.get("LAT") or 0.0
    lon_raw = raw.get("LON") or 0.0
    course_raw = raw.get("COURSE") or 0
    shiptype_raw = raw.get("SHIPTYPE") or 0
    return LiveVesselDTO(
        mmsi=str(raw.get("SHIP_ID", raw.get("MMSI", ""))),
        vessel_name=(raw.get("SHIPNAME") or "").strip() or "UNKNOWN",
        imo_no=raw.get("IMO") or None,
        lat=float(lat_raw),
        lon=float(lon_raw),
        speed_knots=int(speed_raw) / 10.0,
        course=int(course_raw),
        heading=int(raw["HEADING"]) if raw.get("HEADING") else None,
        ship_type_code=int(shiptype_raw),
        ship_type_label=_ship_type_label(int(shiptype_raw)),
        destination=raw.get("DESTINATION") or None,
        flag=raw.get("FLAG") or None,
        length=int(raw["LENGTH"]) if raw.get("LENGTH") else None,
        elapsed_seconds=int(raw["ELAPSED"]) if raw.get("ELAPSED") else None,
    )


# ---------------------------------------------------------------------------
# Tile fetcher
# ---------------------------------------------------------------------------
async def _fetch_tile(client: httpx.AsyncClient, x: int, y: int) -> list[dict]:
    url = _TILE_URL.format(z=_ZOOM, x=x, y=y)
    try:
        resp = await client.get(url, headers=_HEADERS, timeout=8.0)
        resp.raise_for_status()
        payload = resp.json()
        print("payload ------>", payload)
        rows = (payload.get("data") or {}).get("rows") or []
        return rows  # type: ignore[return-value]
    except Exception:
        return []


async def _fetch_all_vessels() -> list[dict]:
    """Fetch the 3×2 tile grid around the JNPA center tile concurrently."""
    tiles = [
        (_CENTER_X + dx, _CENTER_Y + dy)
        for dx in (-1, 0, 1)
        for dy in (0, 1)
    ]
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_fetch_tile(client, x, y) for x, y in tiles])
    # Deduplicate by SHIP_ID (a vessel can appear in two adjacent tiles)
    seen: set[str] = set()
    merged: list[dict] = []
    for rows in results:
        for row in rows:
            key = str(row.get("SHIP_ID", row.get("MMSI", "")))
            if key and key not in seen:
                seen.add(key)
                merged.append(row)
    return merged


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.get(
    "/live",
    response_model=List[LiveVesselDTO],
    summary="Live AIS vessel positions around JNPA (MarineTraffic proxy — no DB write)",
)
async def live_vessels() -> List[LiveVesselDTO]:
    """
    Fetches live AIS data from MarineTraffic's tile API for the 6 tiles
    covering the JNPA / Mumbai port area and returns them as a typed list.
    Results are cached for 60 s. Nothing is written to the database.
    """
    global _cache_ts, _cache_data

    now = time.monotonic()
    if now - _cache_ts < _CACHE_TTL and _cache_data:
        return [_transform(r) for r in _cache_data]

    try:
        raw_rows = await _fetch_all_vessels()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "marinetraffic_fetch_failed", "detail": str(exc)},
        ) from exc

    _cache_ts = now
    _cache_data = raw_rows
    return [_transform(r) for r in raw_rows]
