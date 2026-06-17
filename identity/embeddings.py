"""Deterministic synthetic face-embedding generator + cosine matcher.

DPDP posture: this module SIMULATES the embedding stage of a face-recognition
pipeline. A production system would run a CNN such as ArcFace over a live camera
frame to produce a ~512-d embedding; here we instead derive a deterministic
unit-norm vector from a hash of the ``driver_id`` so the match / threshold /
PROVISIONAL logic downstream is fully provable WITHOUT capturing, storing, or
processing any real biometric data. No images are ever read or written.

The numbers are chosen so the pipeline behaves like a real one:

* A genuine live capture is the enrolment vector plus small deterministic
  per-capture "noise", giving cosine ~0.97 against enrolment (close, not
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
# enrolment vector. Tuned so cosine(enrolment, genuine_capture) ~= 0.97 at
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
        raise ValueError(f"dimension mismatch: {len(a)} != {len(b)}")
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

    * ``genuine=True``  -> the driver's enrolment vector perturbed by small
      deterministic per-capture noise. Cosine vs enrolment ~= 0.97.
    * ``genuine=False`` -> a wholly different deterministic identity (an
      impostor presenting at the gate). Cosine vs the claimed enrolment < 0.5.

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


__all__ = [
    "DEFAULT_DIM",
    "synth_embedding",
    "cosine",
    "capture_embedding",
]
