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
ArcFace) and gates enrollment behind explicit consent — the decision logic here
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

import base64
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Literal, Optional

from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

from jnpa_shared.backbone import PeriodicPublisher
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import TOPIC_FACE, FaceVerification

from .config import IdentityConfig
from .embeddings import build_provider, capture_embedding, cosine, synth_embedding
from .gallery import EnrolledDriver, generate_gallery
from .metrics import VERIFICATIONS, metrics_asgi_app
from .quality import (
    assess_quality,
    liveness_check,
    liveness_enabled,
    liveness_model_loaded,
    quality_enabled,
)

cfg = IdentityConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("identity")

# In-memory deterministic gallery, (re)built at startup.
_GALLERY: Dict[str, EnrolledDriver] = {}

# Pluggable embedding provider (synthetic by default; real ArcFace-ONNX when
# configured). Decision logic below is provider-agnostic — it only sees scores.
_PROVIDER = build_provider(cfg.embedder, cfg.arcface_model_path)

# Production posture: the synthetic (image-free) matcher must never decide a real
# capture in production. If the real model failed to load there, a real frame is
# refused rather than silently passed by the deterministic fallback.
import os as _os  # noqa: E402

_DEV_ENVS = {"development", "dev", "local", "test"}


def _is_production() -> bool:
    return _os.environ.get("APP_ENV", "development").strip().lower() not in _DEV_ENVS


def _synthetic_blocked() -> bool:
    return _is_production() and _PROVIDER.name == "synthetic"


class StartupSafetyError(RuntimeError):
    """A required production model failed to load — abort the boot (fail fast)."""


def _readiness() -> tuple[bool, dict]:
    """Production readiness of the identity service's required models.

    ArcFace (real embedder) MUST be loaded; the liveness model MUST be loaded when
    ``IDENTITY_LIVENESS=true``. In development everything is reported ready (the
    synthetic provider needs no model). Drives the startup gate AND ``/healthz``."""
    if not _is_production():
        return True, {"mode": "development"}
    checks = {
        "arcface": _PROVIDER.name == "onnx",
        "liveness": (not liveness_enabled()) or liveness_model_loaded(),
    }
    return all(checks.values()), checks


def _production_startup_gate() -> None:
    """FAIL FAST: refuse to start in production unless every required model loads.

      * ArcFace ONNX must be the active embedder and load successfully — the
        synthetic (image-free) matcher may NEVER decide a real capture.
      * When IDENTITY_LIVENESS=true the anti-spoof model must load — no spoof may
        pass un-checked.

    A no-op in development. Raised from the lifespan startup so uvicorn aborts the
    boot rather than serving a degraded service."""
    if not _is_production():
        return
    if _PROVIDER.name != "onnx":
        raise StartupSafetyError(
            "APP_ENV is production but the ArcFace ONNX embedder is not active. "
            "Set IDENTITY_EMBEDDER=onnx and IDENTITY_ARCFACE_MODEL=<model.onnx>. "
            "The synthetic matcher must never decide a real capture."
        )
    try:
        _PROVIDER.ensure_loaded()
    except Exception as exc:  # noqa: BLE001
        raise StartupSafetyError(f"ArcFace model failed to load: {exc}") from exc
    if liveness_enabled() and not liveness_model_loaded():
        raise StartupSafetyError(
            "IDENTITY_LIVENESS=true but the anti-spoof model is not loaded. "
            "Mount IDENTITY_LIVENESS_MODEL=<antispoof.onnx>. Liveness is mandatory: "
            "no spoof may pass un-checked."
        )
    log.info("identity_production_models_loaded", arcface=True,
             liveness=liveness_enabled())

# Camera-enrolled reference templates + photo pointers, keyed by driver_id. These
# OVERRIDE the synthetic gallery enrollment once a consented reference is captured
# (so "Capture & Verify" compares against the driver's own enrolled template).
_REFERENCES: Dict[str, List[float]] = {}
_PHOTO_URLS: Dict[str, str] = {}


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
    image: Optional[str] = Field(
        default=None,
        description=(
            "Captured live frame as base64 (optionally a data: URL). When present, "
            "the configured embedding provider embeds it for the match. When absent, "
            "the legacy 'simulate' path is used (kept for tests/back-compat)."
        ),
    )
    simulate: Optional[Literal["genuine", "impostor", "unknown"]] = Field(
        default="genuine",
        description=(
            "Legacy simulated capture selector, used only when no image is supplied: "
            "'genuine' the enrolled driver, 'impostor' someone else, 'unknown' a miss."
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
    # Which embedding provider produced the capture vector ("synthetic" | "onnx").
    provider: str = "synthetic"
    # Biometric gate results (present when a real frame was assessed).
    quality: Optional[dict] = None
    liveness: Optional[dict] = None


class EnrolRequest(BaseModel):
    driver_id: str = Field(..., description="Driver id to (re)enroll a reference for.")
    image: Optional[str] = Field(
        default=None,
        description="Captured reference frame as base64 (optionally a data: URL).",
    )
    photo_url: Optional[str] = Field(
        default=None, description="Optional object-store URL of the stored reference photo."
    )
    overwrite: bool = Field(
        default=False,
        description="Replace an existing reference template. Default False so an "
        "existing driver embedding is never silently clobbered (safety rule); admin "
        "approval / deliberate re-enrollment sets this True.",
    )


class EnrolResponse(BaseModel):
    enrolled: bool
    driver_id: str
    provider: str
    dim: int
    photo_url: Optional[str] = None
    synthetic: bool = True
    reason: Optional[str] = None
    quality: Optional[dict] = None
    # The reference template (unit-norm) so the gateway can persist it for 1:N.
    embedding: Optional[List[float]] = None


# --------------------------------------------------------------------------- #
# Backbone publisher — synthetic face-verification snapshots (Phase C)
# --------------------------------------------------------------------------- #
# Gates the synthetic drivers are scored at; cycled deterministically so the
# board shows verifications spread across the port's terminal gates.
_GATES = ["G-NSICT", "G-GTI", "G-BMCT"]

# Cap the snapshot fan-out so a large gallery doesn't flood the backbone each
# tick (the HTTP /gallery surface still exposes the full enrollment).
_SNAPSHOT_CAP = 100


def _deterministic_score(driver_id: str) -> float:
    """Stable per-driver match_score in [0, 1], skewed toward a clean MATCH.

    Pure function of ``driver_id`` (sha256, no wall-clock / RNG / biometric data
    — identical across runs, hosts, and CI). A first hash byte selects a band so
    the population is realistically distributed:

        ~80% -> [0.90, 1.00)  MATCH        (a confident face-match at the gate)
        ~15% -> [0.50, 0.90)  PROVISIONAL  (low-confidence, admit-on-trust)
         ~5% -> [0.20, 0.50)  NO_MATCH     (the rare mismatch)

    A second hash slice positions the score continuously within the chosen band.
    The band cutoffs line up with the cfg thresholds (verify=0.9, provisional=
    0.5) so ``_result_for`` stays consistent if those are re-tuned via env.
    """
    h = hashlib.sha256(f"face-score|{driver_id}".encode("utf-8")).digest()
    bucket = h[0] / 256.0  # band selector in [0, 1)
    frac = int.from_bytes(h[1:9], "big") / float(1 << 64)  # position in band, [0, 1)
    if bucket < 0.80:
        score = 0.90 + 0.10 * frac
    elif bucket < 0.95:
        score = 0.50 + 0.40 * frac
    else:
        score = 0.20 + 0.30 * frac
    return round(min(1.0, score), 6)


def _result_for(score: float) -> str:
    """Map a score onto MATCH | PROVISIONAL | NO_MATCH using the cfg thresholds."""
    if score >= cfg.verify_threshold:
        return "MATCH"
    if score >= cfg.provisional_threshold:
        return "PROVISIONAL"
    return "NO_MATCH"


def _face_verification_snapshot() -> List[FaceVerification]:
    """One FaceVerification per enrolled (synthetic) driver, for the backbone.

    DPDP: only the synthetic driver_id, gate_id, a deterministic match_score and
    the MATCH/PROVISIONAL/NO_MATCH result go on the wire — never the enrollment
    embedding or any real biometric. ``synthetic=True`` (the schema default).
    """
    drivers = list(_GALLERY)
    if not drivers:
        return []
    if len(drivers) > _SNAPSHOT_CAP:
        log.info(
            "face_snapshot_capped",
            enrolled=len(drivers),
            cap=_SNAPSHOT_CAP,
        )
        drivers = drivers[:_SNAPSHOT_CAP]
    events: List[FaceVerification] = []
    for i, driver_id in enumerate(drivers):
        score = _deterministic_score(driver_id)
        events.append(
            FaceVerification(
                driver_id=driver_id,
                gate_id=_GATES[i % len(_GATES)],
                match_score=score,
                result=_result_for(score),
            )
        )
    return events


# Publishes FaceVerification onto the backbone every few seconds, tagged SIM, so
# the dashboard sees identity as just another live feed (Phase C).
_publisher = PeriodicPublisher(
    "identity", TOPIC_FACE, "jnpa.face.verification", _face_verification_snapshot,
    interval_s=5.0, key_fn=lambda ev: ev.driver_id,
    raw_ref_fn=lambda ev: f"driver://{ev.driver_id}",
)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # FAIL FAST: in production a missing ArcFace/liveness model aborts the boot.
    _production_startup_gate()
    n = _rebuild_gallery()
    log.info("gallery_built", enrolled=n, synthetic=True)
    _publisher.start()
    try:
        yield
    finally:
        await _publisher.stop()


app = FastAPI(
    title="JNPA Identity / Face-Recognition Verifier",
    version="0.1.0",
    lifespan=_lifespan,
)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz(response: Response) -> dict:
    """READY (200) only when every required dependency is up; 503 otherwise.

    In production that means the ArcFace embedder and (when enabled) the liveness
    model are loaded. In development it is always READY."""
    ready, checks = _readiness()
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "service": cfg.service_name,
        "enrolled": len(_GALLERY),
        "synthetic": _PROVIDER.name == "synthetic",
        "provider": _PROVIDER.name,
        "liveness_enabled": liveness_enabled(),
        "checks": checks,
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


def _decode_image(data: Optional[str]) -> Optional[bytes]:
    """Decode a base64 / data-URL image string to raw bytes (None if absent/bad)."""
    if not data:
        return None
    try:
        payload = data.split(",", 1)[1] if data.strip().startswith("data:") else data
        return base64.b64decode(payload)
    except Exception:  # pragma: no cover - malformed client payload
        return None


def _reference_embedding(driver_id: str) -> Optional[List[float]]:
    """The template to match against: a camera-enrolled reference if present,
    otherwise the deterministic gallery enrollment."""
    ref = _REFERENCES.get(driver_id)
    if ref is not None:
        return ref
    enrolled = _GALLERY.get(driver_id)
    return enrolled.embedding if enrolled else None


def _capture_vector(driver_id: str, simulate: str, image: Optional[bytes]) -> tuple[List[float], str]:
    """Produce the live-capture embedding + the provider name that made it.

    With an image -> the configured provider embeds the real frame (degrading to
    synthetic on any provider error). Without an image -> the legacy simulate path
    so existing tests/back-compat keep working.
    """
    if image is not None:
        try:
            return _PROVIDER.embed_capture(driver_id=driver_id, image=image), _PROVIDER.name
        except Exception as exc:  # pragma: no cover - model/runtime dependent
            if _is_production():
                # No silent fallback: a real capture must NEVER be passed by the
                # synthetic matcher. Propagate so /verify returns REJECTED.
                raise
            log.warning("embed_provider_failed_degrading_to_synthetic", error=str(exc))
            return capture_embedding(driver_id, genuine=True), "synthetic"
    genuine = simulate == "genuine"
    return capture_embedding(driver_id, genuine=genuine), "synthetic"


def _verify(driver_id: str, simulate: str, image: Optional[bytes] = None) -> VerifyResponse:
    """Core pure-ish verification: capture -> match -> decide.

    Separated from the route so it is unit-testable without an HTTP client. The
    embedding source is pluggable; the cosine + threshold decision is unchanged.
    """
    # Resolve the driver's reference template: a camera-enrolled reference (set by
    # /enrol — e.g. an admin-approved driver) takes priority, else the synthetic
    # gallery enrollment. Either source counts as "enrolled".
    reference = _reference_embedding(driver_id)

    # Unknown / unenrolled driver: nothing to match against, so we admit
    # provisionally on trust (mirrors the Vahan PROVISIONAL path).
    if reference is None:
        until = _utcnow() + timedelta(hours=cfg.cure_window_h)
        return VerifyResponse(
            driver_id=driver_id,
            matched=False,
            score=0.0,
            decision="PROVISIONAL",
            provisional_until=until.isoformat(),
            cure_window_h=cfg.cure_window_h,
            reason="driver_not_enrolled",
            provider=_PROVIDER.name,
        )

    # Capture (real frame via the provider, or the simulated vector) vs the
    # driver's reference template.
    capture, provider_name = _capture_vector(driver_id, simulate, image)
    score = round(cosine(reference, capture), 6)

    if score >= cfg.verify_threshold:
        return VerifyResponse(
            driver_id=driver_id,
            matched=True,
            score=score,
            decision="VERIFIED",
            cure_window_h=cfg.cure_window_h,
            reason="face_match",
            provider=provider_name,
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
            provider=provider_name,
        )

    return VerifyResponse(
        driver_id=driver_id,
        matched=False,
        score=score,
        decision="REJECTED",
        cure_window_h=cfg.cure_window_h,
        reason="face_mismatch",
        provider=provider_name,
    )


def _gate_rejected(driver_id: str, reason: str, *, quality=None, liveness=None) -> VerifyResponse:
    """A quality/liveness gate failure -> REJECTED (never a match) with the reason."""
    VERIFICATIONS.labels("REJECTED").inc()
    log.info("verification_gate_rejected", driver_id=driver_id, reason=reason)
    return VerifyResponse(
        driver_id=driver_id, matched=False, score=0.0, decision="REJECTED",
        cure_window_h=cfg.cure_window_h, reason=reason, provider=_PROVIDER.name,
        quality=quality, liveness=liveness,
    )


@app.post("/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest) -> VerifyResponse:
    sim = (req.simulate or "genuine").lower()
    if sim not in {"genuine", "impostor", "unknown"}:
        sim = "genuine"
    image = _decode_image(req.image)

    # Biometric gates (real capture + real model only). A poor-quality or spoofed
    # frame must never reach the matcher — it returns REJECTED with a specific
    # reason so the client can prompt a retake, and an impostor can't pass on a
    # blurry frame. Skipped under the synthetic provider (no real pixels to assess).
    quality = liveness = None
    if image is not None and _synthetic_blocked():
        return _gate_rejected(req.driver_id, "synthetic_disabled_in_production")
    if image is not None and _PROVIDER.name != "synthetic":
        if quality_enabled():
            quality = assess_quality(image)
            if not quality.get("ok", True):
                return _gate_rejected(req.driver_id, f"quality:{quality.get('reason')}",
                                      quality=quality)
        liveness = liveness_check(image)
        # STRICT (rule 1): with liveness enabled, an UN-checked frame (model not
        # loaded / errored) or a spoof is REJECTED — never silently passed.
        if liveness_enabled():
            if not liveness.get("checked"):
                return _gate_rejected(req.driver_id, "liveness:model_unavailable",
                                      quality=quality, liveness=liveness)
            if not liveness.get("live", False):
                return _gate_rejected(req.driver_id, "liveness:spoof_detected",
                                      quality=quality, liveness=liveness)

    try:
        result = _verify(req.driver_id, sim, image=image)
    except Exception as exc:  # production embed failure -> fail closed, never synthetic
        log.warning("verify_embed_failed", driver_id=req.driver_id, error=str(exc))
        return _gate_rejected(req.driver_id, "embed_failed",
                              quality=quality, liveness=liveness)
    result.quality = quality
    result.liveness = liveness
    VERIFICATIONS.labels(result.decision).inc()
    log.info(
        "verification",
        driver_id=req.driver_id,
        live_capture=image is not None,
        provider=result.provider,
        decision=result.decision,
        score=result.score,
        quality_ok=None if quality is None else quality.get("ok"),
        liveness=None if liveness is None else liveness.get("reason"),
    )
    return result


class EmbedRequest(BaseModel):
    image: Optional[str] = Field(default=None, description="Captured frame (base64/data URL).")


class EmbedResponse(BaseModel):
    ok: bool
    embedding: Optional[List[float]] = None
    dim: int = 0
    provider: str = "synthetic"
    reason: str = "ok"
    quality: Optional[dict] = None
    liveness: Optional[dict] = None


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    """Quality + liveness gate, then return the captured frame's embedding for 1:N
    identification. ``ok=False`` (no embedding) when a gate fails — the matcher is
    never reached for a poor-quality or spoofed frame."""
    image = _decode_image(req.image)
    if image is None:
        return EmbedResponse(ok=False, reason="no_image", provider=_PROVIDER.name)
    if _synthetic_blocked():
        return EmbedResponse(ok=False, reason="synthetic_disabled_in_production",
                             provider=_PROVIDER.name)
    quality = liveness = None
    if _PROVIDER.name != "synthetic":
        if quality_enabled():
            quality = assess_quality(image)
            if not quality.get("ok", True):
                return EmbedResponse(ok=False, reason=f"quality:{quality.get('reason')}",
                                     provider=_PROVIDER.name, quality=quality)
        liveness = liveness_check(image)
        # STRICT (rule 1): enabled-but-unchecked or spoof -> no embedding returned.
        if liveness_enabled():
            if not liveness.get("checked"):
                return EmbedResponse(ok=False, reason="liveness:model_unavailable",
                                     provider=_PROVIDER.name, quality=quality, liveness=liveness)
            if not liveness.get("live", False):
                return EmbedResponse(ok=False, reason="liveness:spoof_detected",
                                     provider=_PROVIDER.name, quality=quality, liveness=liveness)
    try:
        vec = _PROVIDER.embed_reference(driver_id="probe", image=image)
        provider_name = _PROVIDER.name
    except Exception as exc:  # pragma: no cover
        log.warning("embed_provider_failed", error=str(exc))
        return EmbedResponse(ok=False, reason="embed_failed", provider=_PROVIDER.name,
                             quality=quality, liveness=liveness)
    return EmbedResponse(ok=True, embedding=[float(x) for x in vec], dim=len(vec),
                         provider=provider_name, quality=quality, liveness=liveness)


@app.post("/enrol", response_model=EnrolResponse)
async def enrol(req: EnrolRequest) -> EnrolResponse:
    """Capture/refresh a driver's reference template from a live frame.

    The embedding provider turns the consented reference frame into a template
    stored in-process (overriding the synthetic gallery enrollment). Pixels are not
    persisted; only the template (and an optional object-store photo_url) are kept.
    """
    # Production: the synthetic (image-free) embedder may never mint a real
    # template. The startup gate already blocks a synthetic boot; this is belt-and-
    # braces so a misconfig fails closed instead of enrolling a fake template.
    if _synthetic_blocked():
        return EnrolResponse(enrolled=False, driver_id=req.driver_id,
                             provider=_PROVIDER.name, dim=0,
                             reason="synthetic_disabled_in_production")

    # Safety: never silently overwrite an existing reference template. A repeat
    # enroll (e.g. the gateway's restart self-heal) is idempotent unless overwrite
    # is set (deliberate admin approval / re-enrollment).
    existing = _REFERENCES.get(req.driver_id)
    if existing is not None and not req.overwrite:
        log.info("enroll_skipped_existing_reference", driver_id=req.driver_id)
        return EnrolResponse(
            enrolled=True,
            driver_id=req.driver_id,
            provider=_PROVIDER.name,
            dim=len(existing),
            photo_url=req.photo_url or _PHOTO_URLS.get(req.driver_id),
        )

    image = _decode_image(req.image)

    # Quality gate: never store a blurred / dark / faceless reference template.
    # Real-model pipeline only (synthetic provider ignores pixels).
    quality = None
    if image is not None and quality_enabled() and _PROVIDER.name != "synthetic":
        quality = assess_quality(image)
        if not quality.get("ok", True):
            log.info("enroll_rejected_low_quality", driver_id=req.driver_id,
                     reason=quality.get("reason"))
            return EnrolResponse(
                enrolled=False, driver_id=req.driver_id, provider=_PROVIDER.name,
                dim=0, reason=f"quality:{quality.get('reason')}", quality=quality,
            )

    try:
        embedding = _PROVIDER.embed_reference(driver_id=req.driver_id, image=image)
        provider_name = _PROVIDER.name
    except Exception as exc:  # pragma: no cover - model/runtime dependent
        if _is_production():
            # No silent fallback: refuse the enrollment rather than mint a synthetic
            # template that would later pass any face.
            log.warning("enroll_provider_failed", driver_id=req.driver_id, error=str(exc))
            return EnrolResponse(enrolled=False, driver_id=req.driver_id,
                                 provider=_PROVIDER.name, dim=0, reason="embed_failed",
                                 quality=quality)
        log.warning("enroll_provider_failed_degrading_to_synthetic", error=str(exc))
        embedding = synth_embedding(req.driver_id)
        provider_name = "synthetic"
    _REFERENCES[req.driver_id] = embedding
    if req.photo_url:
        _PHOTO_URLS[req.driver_id] = req.photo_url
    log.info(
        "enrollment",
        driver_id=req.driver_id,
        provider=provider_name,
        dim=len(embedding),
        has_image=image is not None,
    )
    return EnrolResponse(
        enrolled=True,
        driver_id=req.driver_id,
        provider=provider_name,
        dim=len(embedding),
        photo_url=req.photo_url,
        reason="ok",
        quality=quality,
        embedding=[float(x) for x in embedding],
    )


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
