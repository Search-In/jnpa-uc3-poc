#!/usr/bin/env python3
"""Generate synthetic 30s ANPR clips when no CC footage is available.

Each clip shows a static Indian-style number plate ("MH 04 AB 1234") drawn onto
a road-grey background, moving to random positions with varying brightness frame
to frame. Output is H.264-ish MP4 via OpenCV's mp4v writer — enough for the
detector/replay pipeline to produce non-zero events.

Usage:  CLIPS_DIR=./data/clips python _synth_clip.py cam_a.mp4 cam_b.mp4 ...
Deterministic per filename (seeded) so runs are reproducible.
"""
from __future__ import annotations

import hashlib
import os
import sys

import cv2
import numpy as np

W, H = 640, 360
FPS = 25
SECONDS = 30
PLATE_W, PLATE_H = 200, 56


def _seed_for(name: str) -> int:
    return int(hashlib.sha256(name.encode()).hexdigest()[:8], 16)


def _make_plate() -> np.ndarray:
    """A white plate with black 'MH 04 AB 1234' text."""
    plate = np.full((PLATE_H, PLATE_W, 3), 255, dtype=np.uint8)
    cv2.rectangle(plate, (1, 1), (PLATE_W - 2, PLATE_H - 2), (0, 0, 0), 2)
    cv2.putText(plate, "MH04AB1234", (8, 38), cv2.FONT_HERSHEY_SIMPLEX,
                0.9, (0, 0, 0), 2, cv2.LINE_AA)
    return plate


def make_clip(path: str) -> None:
    rng = np.random.default_rng(_seed_for(os.path.basename(path)))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(path, fourcc, FPS, (W, H))
    if not writer.isOpened():
        raise RuntimeError(f"cannot open VideoWriter for {path}")

    plate = _make_plate()
    n_frames = FPS * SECONDS
    for i in range(n_frames):
        # Road-grey background with mild noise + a horizon line.
        bg = np.full((H, W, 3), 90, dtype=np.uint8)
        bg += rng.integers(-12, 12, size=bg.shape, dtype=np.int16).clip(-90, 165).astype(np.uint8)
        cv2.line(bg, (0, H // 2), (W, H // 2), (60, 60, 60), 3)

        # Random plate position + per-frame brightness scaling.
        x = int(rng.integers(20, W - PLATE_W - 20))
        y = int(rng.integers(H // 2, H - PLATE_H - 10))
        bright = float(rng.uniform(0.45, 1.15))
        stamped = np.clip(plate.astype(np.float32) * bright, 0, 255).astype(np.uint8)
        bg[y:y + PLATE_H, x:x + PLATE_W] = stamped

        writer.write(bg)
    writer.release()


def main(argv: list[str]) -> int:
    clips_dir = os.environ.get("CLIPS_DIR", "./data/clips")
    os.makedirs(clips_dir, exist_ok=True)
    names = argv[1:] or ["synthetic.mp4"]
    for name in names:
        out = os.path.join(clips_dir, name)
        if os.path.isfile(out) and os.path.getsize(out) > 1024:
            print(f"  exists, skipping: {out}")
            continue
        make_clip(out)
        print(f"  wrote {out} ({os.path.getsize(out)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
