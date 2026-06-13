"""/checkin — TERTIARY web check-in form for the trucking-app fallback chain.

A tiny HTML page a driver (or gate operator) can use when the in-cab GPS device
is dead and there is no ULIP relay: they self-report the truck's plate, device
id, and current position. The submission is stored in the trucks router's
in-memory ``CHECKINS`` map and becomes the TERTIARY source for
``/api/trucks/{device_id}`` — admitting the vehicle under elevated scrutiny.

    GET  /checkin            -> the HTML form
    POST /checkin            -> accept a submission (form-encoded), store it
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse

from jnpa_shared.schemas import is_valid_plate, normalize_plate

from ..logging import get_logger
from ..state import GatewayState, get_state
from .trucks import CHECKINS

log = get_logger("gateway.checkin")

router = APIRouter(tags=["checkin"])

_FORM_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JNPA Gate Check-in</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 30rem; margin: 2rem auto;
            padding: 0 1rem; color: #111; }}
    h1 {{ font-size: 1.25rem; }}
    label {{ display: block; margin: 0.75rem 0 0.25rem; font-weight: 600; }}
    input {{ width: 100%; padding: 0.5rem; font-size: 1rem; box-sizing: border-box; }}
    button {{ margin-top: 1.25rem; padding: 0.6rem 1.2rem; font-size: 1rem;
              background: #0a5; color: #fff; border: 0; border-radius: 4px; }}
    .note {{ color: #666; font-size: 0.85rem; margin-top: 1rem; }}
  </style>
</head>
<body>
  <h1>JNPA Gate Check-in (manual)</h1>
  <p class="note">Use this only when the in-cab GPS device is unavailable. The
     vehicle will be admitted under <strong>elevated scrutiny</strong>.</p>
  <form method="post" action="/checkin">
    <label for="device_id">Device ID</label>
    <input id="device_id" name="device_id" placeholder="TRK-000001" required>
    <label for="plate">Plate</label>
    <input id="plate" name="plate" placeholder="MH04AB1234" required>
    <label for="lat">Latitude</label>
    <input id="lat" name="lat" type="number" step="any" placeholder="18.9489" required>
    <label for="lon">Longitude</label>
    <input id="lon" name="lon" type="number" step="any" placeholder="72.9492" required>
    <button type="submit">Submit check-in</button>
  </form>
</body>
</html>
"""


@router.get("/checkin", response_class=HTMLResponse)
async def checkin_form() -> str:
    return _FORM_HTML


@router.post("/checkin")
async def checkin_submit(
    device_id: str = Form(...),
    plate: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    state: GatewayState = Depends(get_state),
):
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        return JSONResponse(status_code=422, content={"error": "invalid_plate", "plate": plate})
    record = {
        "device_id": device_id,
        "plate": norm,
        "lat": lat,
        "lon": lon,
        "source": "web-checkin",
        "submitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    CHECKINS[device_id] = record
    log.info("checkin_received", device_id=device_id, plate=norm)
    return {"accepted": True, "device_id": device_id, "record": record,
            "note": "stored as TERTIARY source; vehicle admitted under elevated scrutiny"}
