"""Deterministic synthetic face-embedding generator + cosine matcher.

DPDP posture: this module SIMULATES the embedding stage of a face-recognition
pipeline. A production system would run a CNN such as ArcFace over a live camera
frame to produce a ~512-d embedding; here we instead derive a deterministic
unit-norm vector from a hash of the ``driver_id`` so the match / threshold /
PROVISIONAL logic downstream is fully provable WITHOUT capturing, storing, or
processing any real biometric data. No images are ever read or written.

The numbers are chosen so the pipeline behaves like a real one:

* A genuine live capture is the enrollment vector plus small deterministic
  per-capture "noise", giving cosine ~0.97 against enrollment (close, not
  identical — exactly what a second photo of the same person looks like).
* An impostor capture is a different identity's vector, giving cosine < 0.5.

Everything is pure stdlib (``hashlib`` + ``math``): no numpy, no RNG, no
wall-clock — so embeddings are identical across runs, hosts, and CI.
"""
from __future__ import annotations

import hashlib
import math
from typing import List, Sequence

# Default embedding dimensionality (a real ArcFace head emits 512; 128 is enough
# to make the geometry behave identically while staying cheap to compute/test).
DEFAULT_DIM = 128

# How much per-capture noise a *genuine* live capture carries relative to the
# enrollment vector. Tuned so cosine(enrollment, genuine_capture) ~= 0.97 at
# DEFAULT_DIM (the noise vector's L2 norm scales with sqrt(dim), so the value is
# kept small; verified empirically in tests/test_identity.py).
GENUINE_NOISE = 0.0375


def _hash_stream(seed_str: str) -> "bytes":
    """Return a long deterministic byte stream seeded by ``seed_str``.

    We chain SHA-256 over (seed, counter) blocks so we can draw as many
    pseudo-random-but-deterministic floats as the requested dimension needs.
    """
    out = bytearray()
    counter = 0
    # 32 bytes per SHA-256 block; 4 bytes consumed per float -> 8 floats/block.
    needed_blocks = (DEFAULT_DIM // 8) + 4
    while len(out) < needed_blocks * 32:
        block = hashlib.sha256(f"{seed_str}|{counter}".encode("utf-8")).digest()
        out.extend(block)
        counter += 1
    return bytes(out)


def _floats_from(seed_str: str, dim: int) -> List[float]:
    """Draw ``dim`` deterministic floats in [-1, 1) from the hash stream."""
    stream = _hash_stream(seed_str)
    vals: List[float] = []
    for i in range(dim):
        chunk = stream[i * 4 : i * 4 + 4]
        # Map the 32-bit word to [-1, 1).
        word = int.from_bytes(chunk, "big")
        vals.append((word / 0xFFFFFFFF) * 2.0 - 1.0)
    return vals


def _l2_normalize(vec: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:  # pragma: no cover - vanishingly unlikely for a hash draw
        return list(vec)
    return [x / norm for x in vec]


def synth_embedding(seed_str: str, dim: int = DEFAULT_DIM) -> List[float]:
    """Deterministic unit-norm synthetic embedding for ``seed_str``.

    Same seed -> identical vector. The result is L2-normalised so cosine
    similarity is just the dot product.
    """
    return _l2_normalize(_floats_from(seed_str, dim))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors, clamped to [-1, 1].

    For unit-norm inputs this is the dot product; we still divide by the norms
    so the function is correct for any input. Identical vectors give exactly
    1.0.
    """
    if len(a) != len(b):
        # Different embedding spaces cannot match (e.g. a 128-d synthetic gallery
        # template vs a 512-d ArcFace capture). Treat as "no match" rather than
        # erroring so a real capture against a non-face-enrolled driver is rejected.
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:  # pragma: no cover
        return 0.0
    score = dot / (na * nb)
    # Guard against tiny FP overshoot so identical vectors read as exactly 1.0.
    return max(-1.0, min(1.0, score))


def capture_embedding(driver_id: str, genuine: bool, dim: int = DEFAULT_DIM) -> List[float]:
    """Simulate a *live* capture embedding for a verification attempt.

    * ``genuine=True``  -> the driver's enrollment vector perturbed by small
      deterministic per-capture noise. Cosine vs enrollment ~= 0.97.
    * ``genuine=False`` -> a wholly different deterministic identity (an
      impostor presenting at the gate). Cosine vs the claimed enrollment < 0.5.

    Deterministic: a given ``driver_id`` always yields the same genuine and the
    same impostor capture, so tests and demos are reproducible.
    """
    if genuine:
        base = synth_embedding(driver_id, dim)
        # Deterministic noise vector, scaled small, then re-normalise.
        noise = _floats_from(f"capture-noise|{driver_id}", dim)
        perturbed = [b + GENUINE_NOISE * n for b, n in zip(base, noise)]
        return _l2_normalize(perturbed)
    # Impostor: a different identity entirely.
    return synth_embedding(f"impostor|{driver_id}", dim)


# --------------------------------------------------------------------------- #
# Pluggable embedding providers (production seam — see the audit).
# --------------------------------------------------------------------------- #
# The match/threshold/decision logic in identity.app is provider-agnostic: it
# only ever sees two embeddings and a cosine score. Swapping the provider swaps
# *how* an embedding is produced — nothing downstream changes.
#
#   * SyntheticEmbeddingProvider — the existing deterministic, image-free path.
#     Keeps the demo, tests and CI working offline (no model, no real biometric).
#   * OnnxArcFaceProvider — runs a real ArcFace CNN (ONNX Runtime) over the
#     captured frame. Activated by IDENTITY_EMBEDDER=onnx + IDENTITY_ARCFACE_MODEL.
#     Heavy deps (onnxruntime, opencv, numpy) are imported lazily so the default
#     install stays slim; any failure degrades to synthetic.
from typing import Optional  # noqa: E402  (kept local to the provider section)

log = None  # set lazily to avoid a hard logging dependency in this pure module


class EmbeddingProvider:
    """Strategy interface: turn a (driver_id, optional image) into an embedding."""

    name = "base"

    def embed_reference(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        raise NotImplementedError

    def embed_capture(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        raise NotImplementedError

    def ensure_loaded(self) -> None:
        """Eagerly load any heavy artefacts (model weights, detectors).

        Called by the production startup gate so a missing/corrupt model FAILS THE
        SERVICE BOOT rather than surfacing on the first request. No-op by default
        (the synthetic provider has nothing to load)."""
        return None


class SyntheticEmbeddingProvider(EmbeddingProvider):
    """Deterministic, image-free embeddings (the original PoC behaviour).

    The reference is the driver's enrollment vector; a capture is the same vector
    plus small per-capture noise (cosine ~0.97), so a genuine enroll→verify of the
    same driver clears the verify threshold. Pixels are ignored — no biometric is
    processed — which is exactly the documented synthetic/consented PoC posture.
    """

    name = "synthetic"

    def embed_reference(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        return synth_embedding(driver_id or "anonymous-reference")

    def embed_capture(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        return capture_embedding(driver_id or "anonymous-capture", genuine=True)


class OnnxArcFaceProvider(EmbeddingProvider):
    """Real face embeddings via an ArcFace ONNX model over the captured frame.

    Pipeline: decode -> detect the largest face (OpenCV Haar cascade, bundled — no
    extra model) -> crop with margin -> resize 112x112 -> RGB, ArcFace normalise
    -> ONNX inference -> L2-normalised 512-d embedding. If no face is detected the
    frame is centre-cropped (the client face-guide centres the face) so enrollment
    still works. Requires a captured image; raising on a missing image lets the
    caller fall back to synthetic.

    The same pipeline runs for both the reference (enrollment) and the live capture,
    so a genuine same-person enroll->verify scores high (~0.5-0.9) while a different
    person scores near zero — the threshold (IDENTITY_VERIFY_THRESHOLD, tuned for
    ArcFace, ~0.45) cleanly separates them.
    """

    name = "onnx"

    def __init__(self, model_path: str, input_size: int = 112) -> None:
        self._model_path = model_path
        self._input_size = input_size
        self._session = None  # lazily created ONNX Runtime session
        self._cascade = None  # lazily created Haar face detector

    def _ensure_session(self):
        if self._session is None:
            import onnxruntime as ort  # lazy, optional dependency

            self._session = ort.InferenceSession(
                self._model_path, providers=["CPUExecutionProvider"]
            )
        return self._session

    def _ensure_cascade(self):
        if self._cascade is None:
            import cv2  # lazy, optional

            self._cascade = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
            )
        return self._cascade

    def ensure_loaded(self) -> None:
        """Force the ONNX session + Haar detector to load now (startup gate).

        Raises if the model file is missing/unreadable or onnxruntime/opencv are
        not installed — exactly the fail-fast the production boot wants."""
        self._ensure_session()
        self._ensure_cascade()

    def _crop_face(self, arr):
        """Return the largest detected face (with margin), else a centre crop."""
        import cv2  # lazy, optional

        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        faces = self._ensure_cascade().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(60, 60)
        )
        if len(faces) > 0:
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
            m = int(0.25 * w)
            y0, y1 = max(0, y - m), min(arr.shape[0], y + h + m)
            x0, x1 = max(0, x - m), min(arr.shape[1], x + w + m)
            return arr[y0:y1, x0:x1]
        # No detection -> centre square crop (the face-guide overlay centres it).
        h, w = arr.shape[:2]
        s = min(h, w)
        return arr[(h - s) // 2:(h + s) // 2, (w - s) // 2:(w + s) // 2]

    def _embed(self, image: Optional[bytes]) -> List[float]:
        if not image:
            raise ValueError("onnx embedder requires a captured image frame")
        import cv2  # lazy, optional
        import numpy as np  # lazy, optional

        arr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            raise ValueError("could not decode captured image")
        face = cv2.resize(self._crop_face(arr), (self._input_size, self._input_size))
        # ArcFace expects RGB, scaled to [-1, 1].
        rgb = cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype("float32")
        blob = ((rgb - 127.5) / 127.5).transpose(2, 0, 1)[None, ...]  # NCHW
        session = self._ensure_session()
        out = session.run(None, {session.get_inputs()[0].name: blob})[0][0]
        return _l2_normalize([float(x) for x in out])

    def embed_reference(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        return self._embed(image)

    def embed_capture(self, *, driver_id: str, image: Optional[bytes] = None) -> List[float]:
        return self._embed(image)


def build_provider(embedder: str = "synthetic", model_path: str = "") -> EmbeddingProvider:
    """Construct the configured provider. Construction never fails (ONNX deps are
    imported lazily at embed time); selecting onnx without a model path falls back
    to synthetic immediately."""
    if (embedder or "synthetic").lower() == "onnx" and model_path:
        return OnnxArcFaceProvider(model_path)
    return SyntheticEmbeddingProvider()


__all__ = [
    "DEFAULT_DIM",
    "synth_embedding",
    "cosine",
    "capture_embedding",
    "EmbeddingProvider",
    "SyntheticEmbeddingProvider",
    "OnnxArcFaceProvider",
    "build_provider",
]
