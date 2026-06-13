"""Deterministic Indian-plate sample generator.

Renders a realistic number-plate crop (white HSRP background, black FE-Schrift-ish
text, bolt holes, mild perspective + lighting variation) for a given plate
string and seed. Used to build the held-out evaluation set when no public
Indian-plate dataset is reachable (the same graceful-fallback pattern the rest
of the PoC uses for clips and the Vahan dataset), and as ground truth the
detector + OCR are scored against.

Everything is seeded — no wall-clock RNG — so eval slices are reproducible.
"""
from __future__ import annotations

import hashlib
from typing import List, Tuple

import cv2
import numpy as np

Image = np.ndarray

PLATE_W, PLATE_H = 320, 100


def seed_for(s: str) -> int:
    return int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)


def _format_for_display(plate: str) -> str:
    """Insert conventional spaces: MH04AB1234 -> 'MH 04 AB 1234'."""
    import re

    m = re.match(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{4})$", plate)
    if m:
        return " ".join(m.groups())
    m = re.match(r"^(\d{2})(BH)(\d{4})([A-Z]{1,2})$", plate)
    if m:
        return " ".join(m.groups())
    return plate


def render_plate(plate: str, seed: int | None = None) -> Image:
    """Render a single plate crop (BGR uint8) for ``plate``."""
    if seed is None:
        seed = seed_for(plate)
    rng = np.random.default_rng(seed)

    # White HSRP base with a thin black border + two bolt holes.
    base_val = int(rng.integers(235, 256))
    img = np.full((PLATE_H, PLATE_W, 3), base_val, dtype=np.uint8)
    cv2.rectangle(img, (3, 3), (PLATE_W - 4, PLATE_H - 4), (0, 0, 0), 2)
    for cx in (PLATE_W // 4, 3 * PLATE_W // 4):
        cv2.circle(img, (cx, 12), 4, (40, 40, 40), -1)

    text = _format_for_display(plate)
    # Fit the font scale so the text spans most of the plate width.
    scale = 1.5
    thickness = 3
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, thickness)
    while tw > PLATE_W - 24 and scale > 0.6:
        scale -= 0.1
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, thickness)
    org = ((PLATE_W - tw) // 2, (PLATE_H + th) // 2)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_DUPLEX, scale, (15, 15, 15),
                thickness, cv2.LINE_AA)

    # Mild perspective warp (camera not head-on).
    jitter = 6
    src = np.float32([[0, 0], [PLATE_W, 0], [PLATE_W, PLATE_H], [0, PLATE_H]])
    dst = src + rng.integers(-jitter, jitter + 1, size=src.shape).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(img, M, (PLATE_W, PLATE_H), borderValue=(120, 120, 120))

    # Global brightness scaling (sunlight / shade).
    bright = float(rng.uniform(0.8, 1.1))
    img = np.clip(img.astype(np.float32) * bright, 0, 255).astype(np.uint8)
    return img


def render_scene(plate: str, seed: int | None = None) -> Tuple[Image, Tuple[int, int, int, int]]:
    """Render a full ~640x360 'camera frame' with the plate composited onto a
    road-grey background. Returns (frame, plate_bbox) so a detector can be scored
    against a known ROI."""
    if seed is None:
        seed = seed_for("scene:" + plate)
    rng = np.random.default_rng(seed)
    W, H = 640, 360
    bg = np.full((H, W, 3), int(rng.integers(70, 110)), dtype=np.uint8)
    bg += rng.integers(-10, 10, size=bg.shape, dtype=np.int16).clip(-90, 165).astype(np.uint8)
    cv2.line(bg, (0, H // 2), (W, H // 2), (60, 60, 60), 3)

    plate_img = render_plate(plate, seed=seed)
    ph, pw = plate_img.shape[:2]
    x = int(rng.integers(30, W - pw - 30))
    y = int(rng.integers(H // 2, H - ph - 10))
    bg[y:y + ph, x:x + pw] = plate_img
    return bg, (x, y, x + pw, y + ph)


def build_dataset(plates: List[str], split_seed: int = 1337) -> List[dict]:
    """Produce a list of {plate, frame, bbox} samples for the given plate list."""
    out: List[dict] = []
    for i, plate in enumerate(plates):
        frame, bbox = render_scene(plate, seed=split_seed + i)
        out.append({"plate": plate, "frame": frame, "bbox": bbox})
    return out


__all__ = ["render_plate", "render_scene", "build_dataset", "seed_for", "PLATE_W", "PLATE_H"]
