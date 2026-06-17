"""FastAPI app: face-recognition driver verification (Appendix C #2).

PDP augmentation for the JNPA UC-III Digital Twin: at the gate a driver's live
face capture is matched against an enrolled gallery, and the verification
decision feeds the same admit/PROVISIONAL/reject machinery the Vahan fallback
uses. The endpoint surface:

    GET  /healthz             -> liveness + enrolled count + synthetic flag
    GET  /metrics             (Prometheus, mounted)
    GET  /gallery             -> enrolled drivers (ids/names only, no templates)
    GET  /threshold           -> the configured match/provisional thresholds
    POST /verify              -> VERIFIED | PROVISIONAL | REJECTED for a capture

DPDP posture (docs/ASSUMPTIONS.md "Identity / face-recognition (C2)"): the
gallery and every capture are SYNTHETIC and CONSENTED. No real driver biometrics
are processed. The embedding stage is simulated (see identity.embeddings) so the
match / threshold / PROVISIONAL logic is provable without handling personal
data. A production deployment swaps the synthetic embedder for a CNN (e.g.
ArcFace) and gates enrolment behind explicit consent — the decision logic here
is unchanged.

Decision logic (thresholds in identity.config):
    score >= verify_threshold (0.9)                     -> VERIFIED
    provisional_threshold (0.5) <= score < 0.9, OR an
        unknown driver_id (no gallery entry to match)   -> PROVISIONAL
    score < provisional_threshold (0.5)                 -> REJECTED

PROVISIONAL mirrors the Vahan ``admit_provisional`` path (gateway/provisional.py):
the driver is admitted on trust with a 24h cure window (``provisional_until``),
pending manual verification before the window closes — so a face-match miss never
blocks port operations, it just raises a leash.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, Literal, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

from jnpa_shared.logging import configure_logging, get_logger

from .config import IdentityConfig
from .embeddings import capture_embedding, cosine
from .gallery import EnrolledDriver, generate_gallery
from .metrics import VERIFICATIONS, metrics_asgi_app

cfg = IdentityConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("identity")

# In-memory deterministic gallery, (re)built at startup.
_GALLERY: Dict[str, EnrolledDriver] = {}


def _rebuild_gallery() -> int:
    """Regenerate the deterministic synthetic gallery. Returns enrolled count."""
    global _GALLERY
    _GALLERY = generate_gallery(cfg.gallery_size)
    return len(_GALLERY)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #
class VerifyRequest(BaseModel):
    driver_id: str = Field(..., description="Claimed enrolled driver id, e.g. DRV-0001.")
    claimed: Optional[bool] = Field(
        default=None,
        description="Whether the driver asserts an enrolled identity (informational).",
    )
    simulate: Optional[Literal["genuine", "impostor", "unknown"]] = Field(
        default="genuine",
        description=(
            "Which live capture to simulate for this attempt (PoC only; a real "
            "service reads the camera frame): 'genuine' the enrolled driver, "
            "'impostor' someone else, 'unknown' an impostor-style mismatch."
        ),
    )


class VerifyResponse(BaseModel):
    driver_id: str
    matched: bool
    score: float
    decision: Literal["VERIFIED", "PROVISIONAL", "REJECTED"]
    provisional_until: Optional[str] = None
    cure_window_h: int
    reason: str
    synthetic: bool = True


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    n = _rebuild_gallery()
    log.info("gallery_built", enrolled=n, synthetic=True)
    yield


app = FastAPI(
    title="JNPA Identity / Face-Recognition Verifier",
    version="0.1.0",
    lifespan=_lifespan,
)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": cfg.service_name,
        "enrolled": len(_GALLERY),
        "synthetic": True,
    }


@app.get("/gallery")
async def gallery() -> dict:
    """Enrolled drivers — ids/names/licences only. Raw embeddings never leave."""
    return {
        "enrolled": len(_GALLERY),
        "synthetic": True,
        "consented": True,
        "drivers": [d.public() for d in _GALLERY.values()],
    }


@app.get("/threshold")
async def threshold() -> dict:
    """The configured match/provisional thresholds + cure window."""
    return {
        "verify_threshold": cfg.verify_threshold,
        "provisional_threshold": cfg.provisional_threshold,
        "cure_window_h": cfg.cure_window_h,
        "decisions": {
            "VERIFIED": f"score >= {cfg.verify_threshold}",
            "PROVISIONAL": (
                f"{cfg.provisional_threshold} <= score < {cfg.verify_threshold}"
                " OR unknown driver_id (admit-on-trust, 24h cure window)"
            ),
            "REJECTED": f"score < {cfg.provisional_threshold}",
        },
    }


def _verify(driver_id: str, simulate: str) -> VerifyResponse:
    """Core pure-ish verification: capture -> match -> decide.

    Separated from the route so it is unit-testable without an HTTP client.
    Reads the module-level gallery + cfg; performs no IO.
    """
    enrolled = _GALLERY.get(driver_id)

    # Unknown / unenrolled driver: nothing in the gallery to match against, so
    # we admit provisionally on trust (mirrors the Vahan PROVISIONAL path).
    if enrolled is None:
        until = _utcnow() + timedelta(hours=cfg.cure_window_h)
        return VerifyResponse(
            driver_id=driver_id,
            matched=False,
            score=0.0,
            decision="PROVISIONAL",
            provisional_until=until.isoformat(),
            cure_window_h=cfg.cure_window_h,
            reason="driver_not_enrolled",
        )

    # Simulate a live capture against the claimed enrolment.
    #   genuine  -> the real driver presents (cosine ~0.97)
    #   impostor -> someone else presents     (cosine < 0.5)
    #   unknown  -> treated as an impostor-style capture for scoring
    genuine = simulate == "genuine"
    capture = capture_embedding(driver_id, genuine=genuine)
    score = round(cosine(enrolled.embedding, capture), 6)

    if score >= cfg.verify_threshold:
        return VerifyResponse(
            driver_id=driver_id,
            matched=True,
            score=score,
            decision="VERIFIED",
            cure_window_h=cfg.cure_window_h,
            reason="face_match",
        )

    if score >= cfg.provisional_threshold:
        until = _utcnow() + timedelta(hours=cfg.cure_window_h)
        return VerifyResponse(
            driver_id=driver_id,
            matched=False,
            score=score,
            decision="PROVISIONAL",
            provisional_until=until.isoformat(),
            cure_window_h=cfg.cure_window_h,
            reason="low_confidence_match",
        )

    return VerifyResponse(
        driver_id=driver_id,
        matched=False,
        score=score,
        decision="REJECTED",
        cure_window_h=cfg.cure_window_h,
        reason="face_mismatch",
    )


@app.post("/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest) -> VerifyResponse:
    sim = (req.simulate or "genuine").lower()
    if sim not in {"genuine", "impostor", "unknown"}:
        sim = "genuine"
    result = _verify(req.driver_id, sim)
    VERIFICATIONS.labels(result.decision).inc()
    log.info(
        "verification",
        driver_id=req.driver_id,
        simulate=sim,
        decision=result.decision,
        score=result.score,
    )
    return result


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
