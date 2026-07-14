"""/api/vehicle — Vehicle Intelligence Identity & Detection workflows.

Two camera-driven checks launched from the Vehicle Intelligence RC card:

    POST /api/vehicle/{vehicle_number}/identity  -> face-match the person at the
        vehicle against the ACTIVE driver assigned to it. The driver is resolved
        SERVER-SIDE from the plate (never trusted from the client):
            plate -> jnpa.fleet_vehicles -> vehicle_id
                  -> jnpa.drivers (active assignment) -> driver_id
                  -> identity face template (jnpa.driver_faces)
        The captured frame is matched against THAT driver only. A caller can never
        assert which driver they are; they only supply the plate + the frame.

    POST /api/vehicle/detection  -> ANPR the live frame, extract the number plate,
        and (when an expected plate is supplied) report whether it matches the
        searched vehicle. Reuses the ai/anpr /infer engine (degrades to a synthetic
        read so the demo never blanks).

Reuse only — no new identity/ANPR logic: the face decision goes through the same
identity.verify path (DPDP-audited) and detection through the same ai/anpr engine.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel

from .. import enrollment, fleet
from ..enrollment import decode_data_url, normalize_vehicle_no
from ..logging import get_logger
from ..metrics import REQUESTS
from ..state import GatewayState, get_state
from . import identity as identity_router

log = get_logger("gateway.vehicle_identity")

router = APIRouter(prefix="/api/vehicle", tags=["vehicle-intel"])

# Match threshold: identity.verify maps cosine >= 0.9 -> VERIFIED. We surface the
# same decision, reporting confidence as the cosine score as a percentage.
_VERIFIED = "VERIFIED"


def _plate_key(plate: Optional[str]) -> str:
    """Normalise a plate for comparison: uppercase, strip non-alphanumerics."""
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


def _confidence_pct(score: Any) -> int:
    try:
        return max(0, min(100, round(float(score) * 100)))
    except (TypeError, ValueError):
        return 0


class IdentityBody(BaseModel):
    image: str
    vehicle_number: Optional[str] = None  # informational; path param is authoritative


@router.post("/{vehicle_number}/identity")
async def vehicle_identity(vehicle_number: str, request: Request, body: IdentityBody,
                           state: GatewayState = Depends(get_state)) -> dict:
    """Face-match the captured person against the vehicle's ACTIVE assigned driver.

    Never trusts a client-supplied driver id: the driver is resolved from the
    plate through the Vehicle Master + active assignment, then the frame is matched
    against that driver's enrolled template via the identity service."""
    dsn = state.cfg.postgres_dsn
    plate = (vehicle_number or "").strip()
    if not body.image:
        return {"driver_name": None, "confidence": 0, "status": "NOT_MATCHED",
                "matched": False, "vehicle_number": plate,
                "reason": "no_image", "message": "No camera frame captured."}

    # 1. plate -> Vehicle Master row -> Vehicle ID (the assignment key).
    vehicle = await fleet.find_by_number(dsn, plate)
    vehicle_id = vehicle.get("vehicle_id") if vehicle else None
    if not vehicle_id:
        # Fall back to treating the searched value as a Vehicle ID directly (a user
        # may search the TRK id), so the workflow still resolves for that case.
        maybe = normalize_vehicle_no(plate)
        if await fleet.vehicle_exists(dsn, maybe):
            vehicle_id = maybe
    if not vehicle_id:
        return {"driver_name": None, "confidence": 0, "status": "NOT_MATCHED",
                "matched": False, "vehicle_number": plate,
                "reason": "vehicle_not_registered",
                "message": "Vehicle is not registered in the Vehicle Master."}

    # 2. Vehicle ID -> ACTIVE assigned driver (the PWA-login gate query).
    driver = await enrollment.get_active_driver_by_vehicle(dsn, vehicle_id)
    if not driver:
        return {"driver_name": None, "confidence": 0, "status": "NOT_MATCHED",
                "matched": False, "vehicle_number": plate, "vehicle_id": vehicle_id,
                "reason": "no_active_driver",
                "message": "No active driver linked to this vehicle."}
    driver_id = driver.get("driver_id")

    # 3. Match the captured frame against THAT driver only (identity.verify —
    #    DPDP-audited, real ArcFace when the identity service is up; a real frame is
    #    never passed synthetically). is_synthetic mirrors the PoC posture.
    verify_body: Dict[str, Any] = {
        "driver_id": driver_id, "image": body.image,
        "is_synthetic": True, "purpose": "GATE_VERIFICATION",
    }
    result = await identity_router.verify(request, verify_body, state)
    decision = str(result.get("decision", ""))
    matched = decision == _VERIFIED
    confidence = _confidence_pct(result.get("score"))
    REQUESTS.labels("vehicle-intel", "ok").inc()
    return {
        "driver_name": driver.get("name"),
        "driver_id": driver_id,
        "vehicle_number": vehicle.get("vehicle_number") if vehicle else plate,
        "vehicle_id": vehicle_id,
        "confidence": confidence,
        "status": "MATCHED" if matched else "NOT_MATCHED",
        "matched": matched,
        "decision": decision,
        "reason": result.get("reason"),
        "message": ("Identity verified." if matched
                    else "Face did not match the assigned driver."),
    }


async def _anpr_infer(state: GatewayState, image_bytes: bytes) -> dict:
    """Run the ai/anpr /infer engine on raw image bytes; degrade to a synthetic
    read on failure (same behaviour as /api/anpr/infer)."""
    from ..fallback import AnprPath, SourceState

    url = state.cfg.anpr_ai_url.rstrip("/") + "/infer"
    t0 = time.perf_counter()
    try:
        resp = await state.http.post(
            url, files={"image": ("frame.jpg", image_bytes, "image/jpeg")})
        if resp.status_code == 200:
            await state.record_decision(
                api="anpr", decision_path=AnprPath.LIVE.value,
                latency_ms=(time.perf_counter() - t0) * 1000, source="anpr-ai")
            return {"decision_path": AnprPath.LIVE.value, "record": resp.json()}
        log.info("vehicle_detection_infer_miss", status=resp.status_code)
    except httpx.HTTPError as exc:
        log.warning("vehicle_detection_infer_unreachable", url=url, error=str(exc))
    # Degrade to a deterministic synthetic read.
    from .anpr import _synthetic_read

    await state.record_decision(
        api="anpr", decision_path=AnprPath.SYNTHETIC.value, source="anpr-ai",
        source_state=SourceState.DOWN, ok=False)
    return {"decision_path": AnprPath.SYNTHETIC.value, "record": _synthetic_read("CAM-UPLOAD")}


@router.post("/detection")
async def vehicle_detection(request: Request, body: Dict[str, Any] = Body(...),
                            state: GatewayState = Depends(get_state)) -> dict:
    """ANPR the captured frame and (optionally) compare against the searched plate.

    Body: ``{ image, expected? }`` — ``expected`` (the searched vehicle number)
    lets the backend report ``match`` directly; when absent, ``match`` is null and
    the caller compares client-side."""
    image = body.get("image")
    image_bytes = decode_data_url(image) if image else None
    if not image_bytes:
        return {"detected_vehicle": None, "confidence": 0, "match": None,
                "reason": "no_image", "message": "No camera frame captured."}

    out = await _anpr_infer(state, image_bytes)
    record = out.get("record") or {}
    detected = record.get("plate")
    confidence = _confidence_pct(record.get("conf"))

    expected = body.get("expected") or body.get("vehicle_number")
    match: Optional[bool] = None
    if expected:
        match = bool(detected) and _plate_key(detected) == _plate_key(expected)

    REQUESTS.labels("vehicle-intel", "ok").inc()
    return {
        "detected_vehicle": detected,
        "confidence": confidence,
        "match": match,
        "expected": expected,
        "decision_path": out.get("decision_path"),
        "message": (
            "Vehicle verified." if match
            else "Vehicle mismatch detected." if match is False
            else "Plate detected."),
    }
