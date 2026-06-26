"""Tests for the identity (face-recognition) driver verifier — Appendix C #2.

Everything runs in-process: the embedding/match/decision logic is pure-function
and deterministic (synthetic, consented biometrics only — no real data, no
images, no infra), so the suite stays green in CI without the docker stack.

Covered:
    (a) genuine capture matches the enrolled driver, score >= 0.9 -> VERIFIED
    (b) impostor -> low score -> REJECTED (and would be PROVISIONAL/REJECTED per
        threshold)
    (c) unknown driver -> PROVISIONAL with a 24h cure window
    (d) embeddings are deterministic + unit-norm
    (e) cosine of identical vectors == 1.0
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
IDENTITY_PKG_DIR = REPO_ROOT  # `identity/` is a top-level package off the repo root
for p in (str(SHARED_DIR), str(IDENTITY_PKG_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from starlette.testclient import TestClient  # noqa: E402

from identity.embeddings import (  # noqa: E402
    DEFAULT_DIM,
    capture_embedding,
    cosine,
    synth_embedding,
)
from identity.gallery import generate_gallery  # noqa: E402


# ---------------------------------------------------------------------------
# (d) + (e) Pure-function embedding properties
# ---------------------------------------------------------------------------
def test_embeddings_are_deterministic_and_unit_norm():
    e1 = synth_embedding("DRV-0001")
    e2 = synth_embedding("DRV-0001")
    assert e1 == e2, "same seed must yield an identical embedding"
    assert len(e1) == DEFAULT_DIM
    norm = math.sqrt(sum(x * x for x in e1))
    assert abs(norm - 1.0) < 1e-9, f"embedding must be unit-norm, got {norm}"
    # Different seeds give different (and near-orthogonal) vectors.
    other = synth_embedding("DRV-0002")
    assert e1 != other
    assert cosine(e1, other) < 0.5


def test_cosine_of_identical_vectors_is_one():
    e = synth_embedding("DRV-0007")
    assert cosine(e, e) == 1.0


def test_capture_is_deterministic():
    a = capture_embedding("DRV-0003", genuine=True)
    b = capture_embedding("DRV-0003", genuine=True)
    assert a == b
    imp1 = capture_embedding("DRV-0003", genuine=False)
    imp2 = capture_embedding("DRV-0003", genuine=False)
    assert imp1 == imp2


# ---------------------------------------------------------------------------
# (a) Genuine capture -> high score
# ---------------------------------------------------------------------------
def test_genuine_capture_matches_enrolled_driver():
    """A genuine live capture is close-but-not-identical to enrolment (~0.97)."""
    gallery = generate_gallery(10)
    for driver_id, rec in gallery.items():
        capture = capture_embedding(driver_id, genuine=True)
        score = cosine(rec.embedding, capture)
        assert score >= 0.9, f"{driver_id} genuine score {score} should be VERIFIED"
        assert score < 1.0, "a genuine capture is NOT identical to enrolment"


# ---------------------------------------------------------------------------
# (b) Impostor -> low score
# ---------------------------------------------------------------------------
def test_impostor_capture_scores_low():
    gallery = generate_gallery(10)
    for driver_id, rec in gallery.items():
        capture = capture_embedding(driver_id, genuine=False)
        score = cosine(rec.embedding, capture)
        assert score < 0.5, f"{driver_id} impostor score {score} should be REJECTED"


# ---------------------------------------------------------------------------
# HTTP surface — decision paths
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    import identity.app as appmod
    with TestClient(appmod.app) as c:
        yield c


def test_healthz_reports_synthetic(client):
    resp = client.get("/healthz")
    body = resp.json()
    # Dev is always READY (synthetic provider needs no model); prod returns 503
    # until the ArcFace/liveness models load (rule 4).
    assert resp.status_code == 200
    assert body["status"] == "ready"
    assert body["service"] == "identity"
    assert body["enrolled"] > 0
    assert body["synthetic"] is True


def test_gallery_hides_raw_embeddings(client):
    body = client.get("/gallery").json()
    assert body["enrolled"] == len(body["drivers"])
    assert body["synthetic"] is True
    sample = body["drivers"][0]
    assert set(sample) == {
        "driver_id",
        "name",
        "license_no",
        "synthetic",
        "consented",
        "photo_url",
    }
    assert "embedding" not in sample


def test_threshold_endpoint(client):
    body = client.get("/threshold").json()
    assert body["verify_threshold"] == 0.9
    assert body["provisional_threshold"] == 0.5
    assert body["cure_window_h"] == 24


# (a) end-to-end
def test_verify_genuine_is_verified(client):
    r = client.post("/verify", json={"driver_id": "DRV-0001", "simulate": "genuine"})
    body = r.json()
    assert r.status_code == 200
    assert body["matched"] is True
    assert body["score"] >= 0.9
    assert body["decision"] == "VERIFIED"
    assert body["provisional_until"] is None


# (b) end-to-end
def test_verify_impostor_is_rejected(client):
    r = client.post("/verify", json={"driver_id": "DRV-0001", "simulate": "impostor"})
    body = r.json()
    assert body["matched"] is False
    assert body["score"] < 0.5
    # Per the configured thresholds an impostor's score is below the PROVISIONAL
    # floor, so the decision is REJECTED (sub-0.5 never admits on trust).
    assert body["decision"] == "REJECTED"


# (c) end-to-end
def test_verify_unknown_driver_is_provisional(client):
    r = client.post("/verify", json={"driver_id": "DRV-9999", "simulate": "unknown"})
    body = r.json()
    assert body["matched"] is False
    assert body["decision"] == "PROVISIONAL"
    assert body["reason"] == "driver_not_enrolled"
    assert body["cure_window_h"] == 24
    # 24h cure window, mirroring the Vahan admit_provisional path.
    until = datetime.fromisoformat(body["provisional_until"])
    now = datetime.now(tz=timezone.utc)
    delta_h = (until - now).total_seconds() / 3600.0
    assert 23.0 < delta_h <= 24.0, f"cure window should be ~24h, got {delta_h:.2f}h"


def test_verification_counter_increments(client):
    """Prometheus counter identity_verifications_total{decision} is wired up."""
    client.post("/verify", json={"driver_id": "DRV-0002", "simulate": "genuine"})
    metrics = client.get("/metrics/").text
    assert "identity_verifications_total" in metrics
    assert 'decision="VERIFIED"' in metrics


# ---------------------------------------------------------------------------
# Pluggable embedding provider + camera enrolment (production seam)
# ---------------------------------------------------------------------------
def test_build_provider_defaults_to_synthetic():
    from identity.embeddings import (
        SyntheticEmbeddingProvider,
        OnnxArcFaceProvider,
        build_provider,
    )

    assert isinstance(build_provider(), SyntheticEmbeddingProvider)
    assert isinstance(build_provider("synthetic"), SyntheticEmbeddingProvider)
    # onnx WITHOUT a model path falls back to synthetic (never hard-fails).
    assert isinstance(build_provider("onnx", ""), SyntheticEmbeddingProvider)
    # onnx WITH a model path constructs the real provider (lazy — no load yet).
    assert isinstance(build_provider("onnx", "/models/arcface.onnx"), OnnxArcFaceProvider)


def test_synthetic_provider_reference_matches_capture():
    """Synthetic reference (enrolment) vs a genuine capture clears the threshold."""
    from identity.embeddings import SyntheticEmbeddingProvider, cosine

    p = SyntheticEmbeddingProvider()
    ref = p.embed_reference(driver_id="DRV-0005")
    cap = p.embed_capture(driver_id="DRV-0005")
    assert cosine(ref, cap) >= 0.9


def test_verify_with_image_uses_provider_and_reports_it(client):
    """A captured frame routes through the provider; response names it."""
    import base64

    img = base64.b64encode(b"\xff\xd8\xff\xfake-jpeg").decode()
    r = client.post("/verify", json={"driver_id": "DRV-0002", "image": img})
    body = r.json()
    # Default synthetic provider ignores pixels but still produces a genuine match.
    assert body["decision"] == "VERIFIED"
    assert body["provider"] == "synthetic"


def test_enrol_then_verify_roundtrip(client):
    """Enrolling a reference returns ok; a subsequent verify clears the threshold."""
    import base64

    img = base64.b64encode(b"\xff\xd8\xff\xfake-reference").decode()
    e = client.post("/enrol", json={"driver_id": "DRV-0004", "image": img}).json()
    assert e["enrolled"] is True
    assert e["driver_id"] == "DRV-0004"
    assert e["dim"] > 0
    v = client.post("/verify", json={"driver_id": "DRV-0004", "image": img}).json()
    assert v["decision"] == "VERIFIED"


def test_enrol_new_driver_absent_from_gallery_then_verify(client):
    """A driver NOT in the synthetic gallery (the admin-approval path) becomes
    verifiable once a reference template is enrolled — a reference enrolment alone
    counts as 'enrolled', no gallery row required."""
    import base64

    img = base64.b64encode(b"\xff\xd8\xff\xfake-ref-new-driver").decode()
    new_id = "DRV-ENROLLED-9001"

    # Before enrolment: unknown driver -> PROVISIONAL (not enrolled).
    pre = client.post("/verify", json={"driver_id": new_id, "image": img}).json()
    assert pre["decision"] == "PROVISIONAL"
    assert pre["reason"] == "driver_not_enrolled"

    # Enrol a reference template, then verify -> VERIFIED (reference == enrolled).
    e = client.post("/enrol", json={"driver_id": new_id, "image": img}).json()
    assert e["enrolled"] is True
    v = client.post("/verify", json={"driver_id": new_id, "image": img}).json()
    assert v["decision"] == "VERIFIED"
    assert v["matched"] is True
