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

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Request

from ..dpdp import audit_identity_access, enforce_dpdp
from ..logging import get_logger
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


async def _upstream(state: GatewayState, method: str, path: str,
                    json: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    url = state.cfg.identity_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = await state.http.get(url)
        else:
            resp = await state.http.post(url, json=json or {})
        UPSTREAM_LATENCY.labels("identity", "identity").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("identity_upstream_failed", path=path, error=str(exc))
    return None


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


@router.post("/verify")
async def verify(request: Request, body: Dict[str, Any] = Body(...),
                 state: GatewayState = Depends(get_state)) -> dict:
    # DPDP enforcement (SEC-3): purpose-limitation + synthetic-only in the PoC.
    # PoC requests are synthetic by default; a caller asserting real biometrics is
    # refused unless ALLOW_REAL_BIOMETRICS (post-award, consent-gated) is set.
    is_synthetic = bool(body.get("is_synthetic", True))
    purpose = enforce_dpdp(purpose=body.get("purpose"), is_synthetic=is_synthetic)
    driver_id = body.get("driver_id", "")

    data = await _upstream(state, "POST", "/verify", body)
    if data is not None:
        REQUESTS.labels("identity", "ok").inc()
        audit_identity_access(actor=_actor(request), driver_id=driver_id, purpose=purpose,
                              is_synthetic=is_synthetic, decision=str(data.get("decision", "?")))
        return {"decision_path": "LIVE", "is_synthetic": is_synthetic, "purpose": purpose, **data}
    simulate = body.get("simulate", "genuine")
    result = _local_verify(driver_id, simulate)
    REQUESTS.labels("identity", "ok").inc()
    audit_identity_access(actor=_actor(request), driver_id=driver_id, purpose=purpose,
                          is_synthetic=is_synthetic, decision=str(result.get("decision", "?")))
    return {"decision_path": "SYNTHETIC", "is_synthetic": is_synthetic, "purpose": purpose, **result}


@router.get("/gallery")
async def gallery(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/gallery")
    if data is not None:
        REQUESTS.labels("identity", "ok").inc()
        return {"decision_path": "LIVE", **data}
    from identity import gallery as gal_mod  # type: ignore
    drivers = [d.public() for d in gal_mod.generate_gallery().values()]
    REQUESTS.labels("identity", "ok").inc()
    return {"decision_path": "SYNTHETIC", "synthetic": True,
            "drivers": drivers, "count": len(drivers)}


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
