"""/api/identity — face-recognition driver verification (PDP augmentation,
Appendix C #2).

Proxies the ``identity`` service (port 8360). On failure, runs the same
deterministic embed -> match -> decision in-process so the verification panel
works in the instant demo. A match miss / unknown driver maps to PROVISIONAL
with a 24-hr cure window, mirroring the Vahan ``admit_provisional`` path — the
driver is admitted on trust pending manual verification.

DPDP posture: PoC biometrics are SYNTHETIC, CONSENTED faces only (see
docs/ASSUMPTIONS.md). No real driver biometrics are processed.

    POST /api/identity/verify   -> VERIFIED | PROVISIONAL | REJECTED
    GET  /api/identity/gallery  -> enrolled (synthetic) drivers, ids/names only
    GET  /api/identity/threshold-> configured match thresholds
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from .. import enrollment, objectstore
from ..dpdp import audit_identity_access, enforce_dpdp
from ..logging import get_logger
from ..mode import allow_base64_image_fallback, allow_synthetic_identity
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state


def _actor(request: Request) -> str:
    """Best-effort caller identity for the DPDP audit record."""
    principal = getattr(request.state, "principal", None)
    if principal is not None:
        return f"{principal.role}:{principal.sub}"
    return request.client.host if request.client else "anonymous"

log = get_logger("gateway.identity")

router = APIRouter(prefix="/api/identity", tags=["identity"])

_VERIFY_THRESHOLD = 0.9
_PROVISIONAL_THRESHOLD = 0.5
_CURE_WINDOW_H = 24


# Real ArcFace inference (model load + embed) can take several seconds on CPU,
# well over the 2 s default upstream budget. A short timeout here would make the
# gateway fall back to the synthetic path mid-verification — which would falsely
# pass ANY face — so identity calls get a generous timeout instead.
_IDENTITY_TIMEOUT_S = 20.0


async def _upstream(state: GatewayState, method: str, path: str,
                    json: Dict[str, Any] | None = None,
                    timeout: float | None = None) -> Dict[str, Any] | None:
    url = state.cfg.identity_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = await state.http.get(url, timeout=timeout)
        else:
            resp = await state.http.post(url, json=json or {}, timeout=timeout)
        UPSTREAM_LATENCY.labels("identity", "identity").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("identity_upstream_failed", path=path, error=str(exc))
    return None


def _decide_from_score(driver_id: str, score: float) -> dict:
    """Map a cosine score to the VERIFIED / PROVISIONAL / REJECTED decision."""
    if score >= _VERIFY_THRESHOLD:
        return {"driver_id": driver_id, "matched": True, "score": score,
                "decision": "VERIFIED", "cure_window_h": _CURE_WINDOW_H,
                "reason": "face_match"}
    if score >= _PROVISIONAL_THRESHOLD:
        until = (datetime.now(timezone.utc) + timedelta(hours=_CURE_WINDOW_H)).isoformat()
        return {"driver_id": driver_id, "matched": False, "score": score,
                "decision": "PROVISIONAL", "provisional_until": until,
                "cure_window_h": _CURE_WINDOW_H, "reason": "low_match"}
    return {"driver_id": driver_id, "matched": False, "score": score,
            "decision": "REJECTED", "cure_window_h": _CURE_WINDOW_H,
            "reason": "face_mismatch"}


def _local_enrolled_verify(driver_id: str, simulate: str) -> dict:
    """Deterministic verify for an admin-approved driver that is NOT in the
    synthetic gallery, used when the identity service is unreachable. Mirrors the
    synthetic provider: reference == synth_embedding(driver_id), capture ~0.97."""
    from identity import embeddings  # type: ignore

    genuine = simulate != "impostor"
    reference = embeddings.synth_embedding(driver_id)
    capture = embeddings.capture_embedding(driver_id, genuine=genuine)
    return _decide_from_score(driver_id, round(embeddings.cosine(reference, capture), 6))


def _local_verify(driver_id: str, simulate: str) -> dict:
    """Replicate the identity service's decision deterministically."""
    from identity import embeddings, gallery  # type: ignore

    gal = gallery.generate_gallery()
    enrolled = gal.get(driver_id)
    if enrolled is None:
        # Unknown driver -> admit provisionally on trust (Vahan PROVISIONAL path).
        until = (datetime.now(timezone.utc) + timedelta(hours=_CURE_WINDOW_H)).isoformat()
        return {"driver_id": driver_id, "matched": False, "score": 0.0,
                "decision": "PROVISIONAL", "provisional_until": until,
                "cure_window_h": _CURE_WINDOW_H, "reason": "unknown_driver"}

    genuine = simulate != "impostor"
    capture = embeddings.capture_embedding(driver_id, genuine=genuine)
    score = round(embeddings.cosine(enrolled.embedding, capture), 6)
    if score >= _VERIFY_THRESHOLD:
        return {"driver_id": driver_id, "matched": True, "score": score,
                "decision": "VERIFIED", "cure_window_h": _CURE_WINDOW_H}
    if score >= _PROVISIONAL_THRESHOLD:
        until = (datetime.now(timezone.utc) + timedelta(hours=_CURE_WINDOW_H)).isoformat()
        return {"driver_id": driver_id, "matched": False, "score": score,
                "decision": "PROVISIONAL", "provisional_until": until,
                "cure_window_h": _CURE_WINDOW_H, "reason": "low_match"}
    return {"driver_id": driver_id, "matched": False, "score": score,
            "decision": "REJECTED", "cure_window_h": _CURE_WINDOW_H}


async def _ensure_identity_enrolled(state: GatewayState, driver_id: str) -> bool:
    """Re-push an approved driver's stored reference template into the identity
    service. The identity gallery is in-memory, so after a restart an ACTIVE
    enrolled driver looks "not enrolled"; this self-heals the template from the
    persisted reference frame so verification keeps working across restarts.
    Returns True if a reference was (re-)enrolled."""
    # Prefer the durable master record; fall back to the workflow record (dev).
    rec = await enrollment.get_driver(state.cfg.postgres_dsn, driver_id)
    if not rec:
        wf = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=True)
        rec = wf if wf and wf.get("status") == enrollment.ACTIVE else None
    if not rec:
        return False
    ref = rec.get("reference_image")
    if not ref:
        return False
    data = await _upstream(state, "POST", "/enrol", {
        "driver_id": driver_id, "image": ref,
        "photo_url": rec.get("photo_url"), "is_synthetic": True, "purpose": "ENROLMENT",
    }, timeout=_IDENTITY_TIMEOUT_S)
    return data is not None


@router.post("/verify")
async def verify(request: Request, body: Dict[str, Any] = Body(...),
                 state: GatewayState = Depends(get_state)) -> dict:
    # DPDP enforcement (SEC-3): purpose-limitation + synthetic-only in the PoC.
    # PoC requests are synthetic by default; a caller asserting real biometrics is
    # refused unless ALLOW_REAL_BIOMETRICS (post-award, consent-gated) is set.
    is_synthetic = bool(body.get("is_synthetic", True))
    purpose = enforce_dpdp(purpose=body.get("purpose"), is_synthetic=is_synthetic)
    driver_id = body.get("driver_id", "")

    has_image = bool(body.get("image"))
    data = await _upstream(state, "POST", "/verify", body, timeout=_IDENTITY_TIMEOUT_S)
    # Self-heal: an ACTIVE enrolled driver that the (in-memory) identity service no
    # longer recognises after a restart is re-enrolled from the persisted template,
    # then verification is retried once so it returns a real match decision.
    if (data is not None and data.get("decision") == "PROVISIONAL"
            and str(data.get("reason", "")) in {"driver_not_enrolled", "unknown_driver"}):
        if await _ensure_identity_enrolled(state, driver_id):
            retry = await _upstream(state, "POST", "/verify", body, timeout=_IDENTITY_TIMEOUT_S)
            if retry is not None:
                data = retry

    if data is not None:
        path, result = "LIVE", data
    elif has_image:
        # Identity service unreachable. CRITICAL: a real captured frame must NEVER be
        # passed by the deterministic synthetic match (which keys off driver_id and
        # ignores the face — it would approve any face). When an image was supplied we
        # cannot confirm the face, so admit PROVISIONALLY (manual check) instead.
        until = (datetime.now(timezone.utc) + timedelta(hours=_CURE_WINDOW_H)).isoformat()
        path = "SYNTHETIC"
        result = {"driver_id": driver_id, "matched": False, "score": 0.0,
                  "decision": "PROVISIONAL", "provisional_until": until,
                  "cure_window_h": _CURE_WINDOW_H, "reason": "identity_service_unavailable",
                  "provider": "unavailable"}
    else:
        # No image -> legacy simulate path: deterministic synthetic decision (demo/tests).
        simulate = body.get("simulate", "genuine")
        rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=False)
        if rec and rec.get("status") == enrollment.ACTIVE:
            result = _local_enrolled_verify(driver_id, simulate)
        else:
            result = _local_verify(driver_id, simulate)
        path = "SYNTHETIC"

    actor = _actor(request)
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=actor, driver_id=driver_id, purpose=purpose,
                          is_synthetic=is_synthetic, decision=str(result.get("decision", "?")))
    # Persistent verification audit trail (jnpa.verification_logs).
    await enrollment.log_verification(
        state.cfg.postgres_dsn, driver_id=driver_id,
        decision=str(result.get("decision", "?")), score=result.get("score"),
        matched=result.get("matched"), provider=result.get("provider"),
        decision_path=path, actor=actor, purpose=purpose, reason=result.get("reason"))
    return {"decision_path": path, "is_synthetic": is_synthetic, "purpose": purpose, **result}


_IDENTIFY_THRESHOLD = 0.45  # ArcFace cosine; below -> UNKNOWN driver


@router.post("/identify")
async def identify(request: Request, body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    """1:N identification — captured face -> embedding -> nearest enrolled driver.

    Pipeline: quality + liveness gate (identity /embed) -> ArcFace embedding ->
    cosine nearest-neighbour over jnpa.driver_faces -> top-1 if >= threshold else
    UNKNOWN. No manual driver selection. Every attempt is audited.
    """
    from identity.embeddings import cosine  # type: ignore

    is_synthetic = bool(body.get("is_synthetic", True))
    purpose = enforce_dpdp(purpose=body.get("purpose") or "GATE_VERIFICATION",
                           is_synthetic=is_synthetic)
    if not body.get("image"):
        raise HTTPException(status_code=400, detail="image required for identification")

    data = await _upstream(state, "POST", "/embed", {"image": body["image"]},
                           timeout=_IDENTITY_TIMEOUT_S)
    actor = _actor(request)
    if data is None:
        # Identity service down: a real capture is never matched synthetically.
        if not allow_synthetic_identity():
            raise HTTPException(status_code=503, detail={
                "error": "identity_service_unavailable", "component": "identity",
                "message": "Identity service required for 1:N identification"})
        result = {"decision": "PROVISIONAL", "driver_id": None, "score": 0.0,
                  "reason": "identity_service_unavailable", "provider": "unavailable"}
        await enrollment.log_verification(
            state.cfg.postgres_dsn, driver_id="*", decision="PROVISIONAL", score=0.0,
            matched=False, provider="unavailable", decision_path="SYNTHETIC",
            actor=actor, purpose=purpose, reason=result["reason"])
        return {"decision_path": "SYNTHETIC", "purpose": purpose, **result}

    # Quality / liveness gate failed -> stop, no match.
    if not data.get("ok"):
        result = {"decision": "REJECTED", "driver_id": None, "score": 0.0,
                  "matched": False, "reason": data.get("reason"),
                  "provider": data.get("provider"), "quality": data.get("quality"),
                  "liveness": data.get("liveness")}
        await enrollment.log_verification(
            state.cfg.postgres_dsn, driver_id="*", decision="REJECTED", score=0.0,
            matched=False, provider=data.get("provider"), decision_path="LIVE",
            actor=actor, purpose=purpose, reason=data.get("reason"))
        return {"decision_path": "LIVE", "purpose": purpose, **result}

    # 1:N nearest-neighbour over the biometric template store.
    probe = data["embedding"]
    faces = await enrollment.load_faces(state.cfg.postgres_dsn)
    best_id, best_score = None, -1.0
    for f in faces:
        emb = f.get("embedding") or []
        if len(emb) != len(probe):
            continue
        sc = cosine(probe, emb)
        if sc > best_score:
            best_id, best_score = f.get("driver_id"), sc
    matched = best_id is not None and best_score >= _IDENTIFY_THRESHOLD
    decision = "VERIFIED" if matched else "REJECTED"
    result = {
        "decision": decision,
        "driver_id": best_id if matched else None,
        "candidate_id": best_id,
        "score": round(float(best_score), 6) if faces else 0.0,
        "matched": matched,
        "reason": "identified" if matched else "unknown_driver",
        "provider": data.get("provider"),
        "gallery_size": len(faces),
        "quality": data.get("quality"),
        "liveness": data.get("liveness"),
    }
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=actor, driver_id=best_id or "*", purpose=purpose,
                          is_synthetic=is_synthetic, decision=decision)
    await enrollment.log_verification(
        state.cfg.postgres_dsn, driver_id=best_id or "*", decision=decision,
        score=result["score"], matched=matched, provider=data.get("provider"),
        decision_path="LIVE", actor=actor, purpose=purpose, reason=result["reason"])
    return {"decision_path": "LIVE", "purpose": purpose, **result}


@router.post("/enrol")
async def enrol(request: Request, body: Dict[str, Any] = Body(...),
                state: GatewayState = Depends(get_state)) -> dict:
    """Capture/refresh a driver's reference template (purpose = ENROLMENT).

    DPDP-gated like /verify; proxies the identity service, degrading to an
    in-process synthetic reference so the demo enrolls even if the service is down.
    """
    is_synthetic = bool(body.get("is_synthetic", True))
    purpose = enforce_dpdp(purpose=body.get("purpose") or "ENROLMENT", is_synthetic=is_synthetic)
    driver_id = body.get("driver_id", "")

    data = await _upstream(state, "POST", "/enrol", body)
    if data is not None:
        REQUESTS.labels("identity", "ok").inc()
        audit_identity_access(actor=_actor(request), driver_id=driver_id, purpose=purpose,
                              is_synthetic=is_synthetic, decision="ENROLLED")
        return {"decision_path": "LIVE", "is_synthetic": is_synthetic, "purpose": purpose, **data}
    # Identity service down. Production must mint a real template — no synthetic
    # reference fallback (it would later pass any face).
    if not allow_synthetic_identity():
        raise HTTPException(
            status_code=503,
            detail={"error": "identity_service_unavailable", "component": "identity",
                    "message": "Identity service required to mint the enrollment template"})
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=_actor(request), driver_id=driver_id, purpose=purpose,
                          is_synthetic=is_synthetic, decision="ENROLLED")
    return {"decision_path": "SYNTHETIC", "is_synthetic": is_synthetic, "purpose": purpose,
            "enrolled": True, "driver_id": driver_id, "provider": "synthetic",
            "reason": "synthetic_reference"}


async def _merge_enrolled(state: GatewayState, drivers: list) -> list:
    """Append ACTIVE master drivers (jnpa.drivers — promoted on approval) to the
    synthetic gallery so the verification dropdown offers real enrolled drivers too.
    Synthetic ids are kept; enrolled ids are de-duplicated against them."""
    try:
        enrolled = await enrollment.list_active_drivers(state.cfg.postgres_dsn)
    except Exception:  # noqa: BLE001
        return drivers
    existing = {d.get("driver_id") for d in drivers}
    for e in enrolled:
        if e.get("driver_id") in existing:
            continue
        drivers.append({
            "driver_id": e.get("driver_id"),
            "name": e.get("name"),
            "license_no": e.get("license_no") or "",
            "photo_url": e.get("photo_url"),
            "enrolled": True,
        })
    return drivers


@router.get("/gallery")
async def gallery(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/gallery")
    if data is not None:
        drivers = await _merge_enrolled(state, list(data.get("drivers", [])))
        REQUESTS.labels("identity", "ok").inc()
        return {"decision_path": "LIVE", **data, "drivers": drivers, "count": len(drivers)}
    from identity import gallery as gal_mod  # type: ignore
    drivers = await _merge_enrolled(state, [d.public() for d in gal_mod.generate_gallery().values()])
    REQUESTS.labels("identity", "ok").inc()
    return {"decision_path": "SYNTHETIC", "synthetic": True,
            "drivers": drivers, "count": len(drivers)}


# --------------------------------------------------------------------------- enrollment workflow
# Driver PWA submits a profile + consented reference frames -> PENDING; an admin
# (DTCCC_ADMIN / CUSTOMS) reviews and approves -> the identity template is minted
# and the driver becomes ACTIVE (verifiable). DPDP-audited at every step.

@router.post("/enrol-request")
async def enrol_request(request: Request, body: Dict[str, Any] = Body(...),
                        state: GatewayState = Depends(get_state)) -> dict:
    """Driver-side enrollment submission (purpose = ENROLMENT). Stores a PENDING
    request; the driver is NOT activated until an admin approves."""
    is_synthetic = bool(body.get("is_synthetic", True))
    purpose = enforce_dpdp(purpose=body.get("purpose") or "ENROLMENT", is_synthetic=is_synthetic)
    driver_id = str(body.get("driver_id") or "").strip()
    if not driver_id:
        raise HTTPException(status_code=400, detail="driver_id required")
    if not bool(body.get("consent", False)):
        raise HTTPException(status_code=400, detail="biometric consent is required")
    images = body.get("images") or ([body["image"]] if body.get("image") else [])
    images = [i for i in images if i]
    if not images:
        raise HTTPException(status_code=400, detail="at least one reference image is required")

    rec = await enrollment.submit(
        state.cfg.postgres_dsn,
        driver_id=driver_id,
        name=str(body.get("name") or "").strip() or driver_id,
        license_no=str(body.get("license_no") or "").strip(),
        mobile=str(body.get("mobile") or "").strip(),
        vehicle_no=str(body.get("vehicle_no") or "").strip(),
        aadhaar_masked=str(body.get("aadhaar") or body.get("aadhaar_masked") or "").strip(),
        emergency_contact=str(body.get("emergency_contact") or "").strip(),
        consent=True,
        face_images=images,
        documents=body.get("documents") or [],
    )
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=_actor(request), driver_id=driver_id, purpose=purpose,
                          is_synthetic=is_synthetic, decision="ENROL_REQUESTED")
    return {"submitted": True, "status": rec.get("status", enrollment.PENDING),
            "driver_id": driver_id, "enrollment": rec}


@router.get("/enrol-request/{driver_id}")
async def enrol_request_status(driver_id: str,
                               state: GatewayState = Depends(get_state)) -> dict:
    """Driver polls their own enrollment status (PENDING / ACTIVE / REJECTED)."""
    rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=False)
    if not rec:
        raise HTTPException(status_code=404, detail="no enrollment request for this driver")
    return rec


@router.get("/enrollments")
async def list_enrollments(status: Optional[str] = Query(default=None),
                           state: GatewayState = Depends(get_state)) -> dict:
    """Admin queue of enrollment requests (summary view, newest first)."""
    items = await enrollment.list_requests(
        state.cfg.postgres_dsn, status=status.upper() if status else None)
    REQUESTS.labels("identity", "ok").inc()
    return {"enrollments": items, "count": len(items)}


@router.get("/enrollments/{driver_id}")
async def enrollment_detail(driver_id: str,
                            state: GatewayState = Depends(get_state)) -> dict:
    """Full enrollment record incl. the captured reference frames for admin review."""
    rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=True)
    if not rec:
        raise HTTPException(status_code=404, detail="enrollment not found")
    return rec


@router.get("/drivers")
async def list_drivers(state: GatewayState = Depends(get_state)) -> dict:
    """Active master drivers (jnpa.drivers) — the canonical enrolled identities."""
    items = await enrollment.list_active_drivers(state.cfg.postgres_dsn)
    REQUESTS.labels("identity", "ok").inc()
    return {"drivers": items, "count": len(items)}


# --------------------------------------------------------------------------- admin driver-profile creation
# A Control-Room admin (CUSTOMS / DTCCC_ADMIN — enforced by the /api/identity RBAC
# policy) can create a driver profile directly and assign it a Vehicle ID. This
# produces a PENDING enrollment (source=ADMIN) that flows through the SAME approval
# workflow; on approval the driver is promoted to jnpa.drivers and the assigned
# Vehicle ID becomes eligible for PWA login. The vehicle list is the truck fleet;
# already-assigned vehicles are excluded so an admin can only pick an available one.

_VEHICLE_ID_RE = re.compile(r"^TRK-\d{6}$")


class CreateDriverBody(BaseModel):
    name: str
    vehicle_no: str
    license_no: Optional[str] = None
    mobile: Optional[str] = None
    emergency_contact: Optional[str] = None
    driver_id: Optional[str] = None  # optional; auto-generated when absent


async def _fleet_vehicles(state: GatewayState, limit: int) -> List[dict]:
    """Fleet vehicle snapshots from the truck-sim (the vehicle registry in the
    PoC). Degrades to an empty list if the sim is unreachable."""
    url = state.cfg.truck_api_url.rstrip("/") + "/devices/list"
    try:
        resp = await state.http.get(url, params={"limit": str(limit)})
    except httpx.HTTPError as exc:
        log.warning("available_vehicles_fleet_unreachable", error=str(exc))
        return []
    if resp.status_code == 200:
        return list(resp.json().get("devices", []))
    return []


async def _vehicle_exists(state: GatewayState, vehicle_id: str) -> bool:
    """True if the Vehicle ID is a known fleet vehicle. Fail-closed: if the vehicle
    registry (truck-sim) is unreachable we 503 rather than accept an unverifiable id."""
    url = state.cfg.truck_api_url.rstrip("/") + f"/devices/{vehicle_id}"
    try:
        resp = await state.http.get(url)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "vehicle_registry_unavailable",
                    "message": "Cannot verify the vehicle right now; try again shortly."},
        ) from exc
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    raise HTTPException(
        status_code=503,
        detail={"error": "vehicle_registry_unavailable",
                "message": "Vehicle registry returned an unexpected status."})


@router.get("/available-vehicles")
async def available_vehicles(q: Optional[str] = Query(default=None),
                             limit: int = Query(default=50, ge=1, le=500),
                             state: GatewayState = Depends(get_state)) -> dict:
    """Fleet vehicles NOT already assigned to an active driver or open enrollment —
    the source for the Control-Room 'assign vehicle' dropdown. ``q`` filters by
    Vehicle ID / plate substring."""
    taken = await enrollment.assigned_vehicles(state.cfg.postgres_dsn)
    fleet = await _fleet_vehicles(state, max(limit * 4, 200))
    needle = (q or "").strip().upper()
    out: List[dict] = []
    for dev in fleet:
        vid = enrollment.normalize_vehicle_no(dev.get("device_id"))
        if not vid or vid in taken:
            continue
        if needle and needle not in vid and needle not in str(dev.get("plate") or "").upper():
            continue
        out.append({"vehicle_id": dev.get("device_id"), "plate": dev.get("plate"),
                    "state": dev.get("state")})
        if len(out) >= limit:
            break
    REQUESTS.labels("identity", "ok").inc()
    return {"vehicles": out, "count": len(out)}


@router.post("/drivers")
async def create_driver_profile(request: Request, body: CreateDriverBody,
                                state: GatewayState = Depends(get_state)) -> dict:
    """Create an admin-originated driver profile + vehicle assignment (PENDING).

    Validates the vehicle exists and is not already assigned, then records a
    PENDING enrollment (source=ADMIN). Approval is NOT bypassed — the existing
    approve endpoint promotes the driver to ACTIVE. Admin-only via RBAC."""
    actor = _actor(request)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    vehicle_no = enrollment.normalize_vehicle_no(body.vehicle_no)
    if not _VEHICLE_ID_RE.match(vehicle_no):
        raise HTTPException(status_code=400,
                            detail="vehicle_no must be a valid Vehicle ID, e.g. TRK-000123")
    if not await _vehicle_exists(state, vehicle_no):
        raise HTTPException(status_code=404,
                            detail=f"vehicle {vehicle_no} is not in the fleet")
    conflict = await enrollment.vehicle_assignment_conflict(state.cfg.postgres_dsn, vehicle_no)
    if conflict:
        raise HTTPException(status_code=409, detail={
            "error": "vehicle_already_assigned", "vehicle_no": vehicle_no,
            "held_by": conflict,
            "message": f"Vehicle {vehicle_no} is already assigned to "
                       f"{conflict.get('name') or conflict.get('driver_id')} "
                       f"({str(conflict.get('kind'))})."})
    driver_id = (body.driver_id or "").strip() or f"DRV-{uuid.uuid4().hex[:8].upper()}"
    if await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=False):
        raise HTTPException(status_code=409, detail=f"driver_id {driver_id} already exists")

    rec = await enrollment.submit(
        state.cfg.postgres_dsn, driver_id=driver_id, name=name,
        license_no=(body.license_no or "").strip(),
        mobile=(body.mobile or "").strip(),
        vehicle_no=vehicle_no,
        emergency_contact=(body.emergency_contact or "").strip(),
        consent=False, face_images=[], documents=[],
        source=enrollment.SOURCE_ADMIN, created_by=actor)
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=actor, driver_id=driver_id, purpose="ENROLMENT",
                          is_synthetic=True, decision="ADMIN_CREATED")
    return {"created": True, "driver_id": driver_id,
            "status": rec.get("status", enrollment.PENDING), "enrollment": rec}


@router.get("/verifications")
async def verification_log(driver_id: Optional[str] = Query(default=None),
                           limit: int = Query(default=50),
                           state: GatewayState = Depends(get_state)) -> dict:
    """Verification audit trail (jnpa.verification_logs) — who/decision/score/when."""
    items = await enrollment.recent_verifications(
        state.cfg.postgres_dsn, driver_id=driver_id, limit=min(limit, 500))
    REQUESTS.labels("identity", "ok").inc()
    return {"verifications": items, "count": len(items)}


@router.post("/enrollments/{driver_id}/approve")
async def approve_enrollment(driver_id: str, request: Request,
                             state: GatewayState = Depends(get_state)) -> dict:
    """Approve: mint the face template (identity /enrol), persist the reference
    photo to MinIO, and activate the driver for verification."""
    purpose = enforce_dpdp(purpose="ENROLMENT", is_synthetic=True)
    rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=True)
    if not rec:
        raise HTTPException(status_code=404, detail="enrollment not found")
    faces = rec.get("face_images") or []
    reference_image = faces[0] if faces else rec.get("reference_image")
    if not reference_image:
        # A PWA submission MUST carry a reference frame (face enrollment is the whole
        # point). An ADMIN-created profile has none by design: it is approved
        # "profile-only" — activated + promoted to the master driver table so the
        # assigned Vehicle ID becomes eligible for PWA login, with no biometric
        # template. Face enrollment can be completed later from the PWA.
        if str(rec.get("source") or "").upper() != enrollment.SOURCE_ADMIN:
            raise HTTPException(status_code=400, detail="no reference frame to enroll")
        actor = _actor(request)
        # Promote FIRST: the jnpa.drivers insert is what enforces one-active-driver-
        # per-vehicle (uq_drivers_vehicle_active). If it conflicts we abort before
        # flipping the enrollment to ACTIVE, so the record never lands in an
        # inconsistent "ACTIVE enrollment, no driver row" state.
        try:
            await enrollment.promote_to_driver(
                state.cfg.postgres_dsn, rec, actor=actor, photo_url=None,
                reference_image=None, template_dim=None, provider="admin")
        except Exception as exc:  # noqa: BLE001 — most likely uq_drivers_vehicle_active
            log.warning("admin_profile_promote_failed", driver_id=driver_id, error=str(exc))
            raise HTTPException(
                status_code=409,
                detail={"error": "vehicle_already_assigned",
                        "message": "This vehicle is already assigned to another active "
                                   "driver; cannot activate."}) from exc
        updated = await enrollment.mark_active(
            state.cfg.postgres_dsn, driver_id, actor=actor, photo_url=None,
            reference_image=None, template_dim=None, provider="admin")
        REQUESTS.labels("identity", "ok").inc()
        audit_identity_access(actor=actor, driver_id=driver_id, purpose=purpose,
                              is_synthetic=True, decision="APPROVED_PROFILE_ONLY")
        return {"approved": True, "profile_only": True, "enrollment": updated}

    # Reference photo -> MinIO (drivers/ bucket). In PRODUCTION object storage is
    # REQUIRED: a failed upload is a hard error (no base64 fallback). In DEV the
    # base64 frame is kept in the record so the demo works without MinIO.
    photo_url = objectstore.put_reference_photo(
        driver_id, enrollment.decode_data_url(reference_image) or b"")
    if photo_url is None and not allow_base64_image_fallback():
        raise HTTPException(
            status_code=503,
            detail={"error": "object_store_unavailable",
                    "component": "minio",
                    "message": "MinIO is required in production to store the reference photo"})
    # In production the durable pointer is the MinIO URL only; never persist pixels.
    stored_ref = reference_image if allow_base64_image_fallback() else None

    # Mint + store the template in the identity service (reuses its /enrol).
    # overwrite=True: admin approval is the authoritative, deliberate (re-)enrollment.
    data = await _upstream(state, "POST", "/enrol", {
        "driver_id": driver_id, "image": reference_image, "photo_url": photo_url,
        "is_synthetic": True, "purpose": "ENROLMENT", "overwrite": True,
    }, timeout=_IDENTITY_TIMEOUT_S)
    if data is None and not allow_synthetic_identity():
        # Production requires the real ArcFace template to be minted on approval.
        raise HTTPException(
            status_code=503,
            detail={"error": "identity_service_unavailable",
                    "component": "identity",
                    "message": "Identity service must mint the face template before approval"})
    # Quality gate: the identity service refuses to enroll a poor reference frame.
    if data is not None and data.get("enrolled") is False:
        raise HTTPException(
            status_code=422,
            detail={"error": "reference_quality_failed",
                    "reason": data.get("reason"),
                    "quality": data.get("quality"),
                    "message": "Reference frame failed the face-quality check; "
                               "request re-enrollment with a clearer photo"})
    provider = (data or {}).get("provider", "synthetic")
    dim = (data or {}).get("dim")

    actor = _actor(request)
    updated = await enrollment.mark_active(
        state.cfg.postgres_dsn, driver_id, actor=actor, photo_url=photo_url,
        reference_image=stored_ref, template_dim=dim, provider=provider)
    # Promote into the canonical master identity table (jnpa.drivers).
    await enrollment.promote_to_driver(
        state.cfg.postgres_dsn, {**rec, "photo_url": photo_url}, actor=actor,
        photo_url=photo_url, reference_image=stored_ref, template_dim=dim, provider=provider)
    # Persist the biometric template for 1:N identification (jnpa.driver_faces).
    emb = (data or {}).get("embedding")
    if emb:
        await enrollment.store_face(state.cfg.postgres_dsn, driver_id, emb,
                                    dim=dim or len(emb), provider=provider)
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=actor, driver_id=driver_id, purpose=purpose,
                          is_synthetic=True, decision="APPROVED")
    return {"approved": True, "enrollment": updated, "identity": data}


@router.post("/enrollments/{driver_id}/reject")
async def reject_enrollment(driver_id: str, request: Request,
                            body: Dict[str, Any] = Body(default={}),
                            state: GatewayState = Depends(get_state)) -> dict:
    """Reject an enrollment. The driver may re-submit from the PWA."""
    rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=False)
    if not rec:
        raise HTTPException(status_code=404, detail="enrollment not found")
    updated = await enrollment.set_status(
        state.cfg.postgres_dsn, driver_id, enrollment.REJECTED,
        actor=_actor(request), reason=str(body.get("reason") or ""))
    REQUESTS.labels("identity", "ok").inc()
    return {"rejected": True, "enrollment": updated}


@router.post("/enrollments/{driver_id}/reenroll")
async def request_reenrollment(driver_id: str, request: Request,
                               body: Dict[str, Any] = Body(default={}),
                               state: GatewayState = Depends(get_state)) -> dict:
    """Ask the driver to re-capture and re-submit their reference frames."""
    rec = await enrollment.get(state.cfg.postgres_dsn, driver_id, include_faces=False)
    if not rec:
        raise HTTPException(status_code=404, detail="enrollment not found")
    updated = await enrollment.set_status(
        state.cfg.postgres_dsn, driver_id, enrollment.REENROLL,
        actor=_actor(request), reason=str(body.get("reason") or "re-enrollment requested"))
    REQUESTS.labels("identity", "ok").inc()
    return {"reenroll": True, "enrollment": updated}


@router.get("/threshold")
async def threshold(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/threshold")
    if data is not None:
        REQUESTS.labels("identity", "ok").inc()
        return {"decision_path": "LIVE", **data}
    REQUESTS.labels("identity", "ok").inc()
    return {"decision_path": "SYNTHETIC",
            "verify_threshold": _VERIFY_THRESHOLD,
            "provisional_threshold": _PROVISIONAL_THRESHOLD,
            "cure_window_h": _CURE_WINDOW_H}
