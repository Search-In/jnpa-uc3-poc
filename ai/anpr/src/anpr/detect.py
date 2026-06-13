"""YOLOv8 license-plate ROI detector.

We do NOT retrain from scratch. The detector loads a YOLOv8n model fine-tuned
for license-plate localisation, released publicly by computervisioneng:

    https://github.com/computervisioneng/automatic-number-plate-recognition-python-yolov8

The weights file (``license_plate_detector.pt``) is expected under
``resources/`` (downloaded once by ``finetune.py``/the Dockerfile or fetched
from the MinIO ``models`` bucket). On startup we **hash-verify** the file
against ``ANPR_YOLO_SHA256`` if that env/known hash is provided, logging a loud
warning on mismatch.

If ultralytics / torch / the weights are unavailable, the detector degrades to a
classical contrast-based plate finder so the service still answers ``/infer``
(``degraded=True``) — matching the no-ML fallback pattern used throughout the
PoC (see ``ingest/anpr``).
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from jnpa_shared.logging import get_logger

log = get_logger("anpr.detect")

Image = np.ndarray

_RESOURCES = Path(__file__).resolve().parents[2] / "resources"
DEFAULT_WEIGHTS = _RESOURCES / "license_plate_detector.pt"

# Author-published SHA-256 of license_plate_detector.pt. Override via env when a
# different release is pinned. When blank, verification is skipped (logged).
KNOWN_WEIGHTS_SHA256 = os.environ.get("ANPR_YOLO_SHA256", "").strip().lower()


@dataclass
class Detection:
    bbox: Tuple[int, int, int, int]  # x1,y1,x2,y2 of the plate ROI in the frame
    conf: float
    degraded: bool = False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class PlateDetector:
    """Lazy-loading YOLOv8 plate detector with a classical fallback."""

    def __init__(self, weights: Optional[str] = None, conf: float = 0.25) -> None:
        self.weights_path = Path(weights) if weights else DEFAULT_WEIGHTS
        self.conf = conf
        self._model = None
        self._ml_ok: Optional[bool] = None
        self.weights_sha256: Optional[str] = None

    # -- weights integrity --------------------------------------------------
    def verify_weights(self) -> bool:
        """Hash-verify the weights file on startup. Returns False if the file is
        missing or (when a known hash is configured) the digest mismatches."""
        if not self.weights_path.is_file():
            log.warning("yolo_weights_missing", path=str(self.weights_path))
            return False
        self.weights_sha256 = sha256_file(self.weights_path)
        if KNOWN_WEIGHTS_SHA256:
            if self.weights_sha256 != KNOWN_WEIGHTS_SHA256:
                log.error(
                    "yolo_weights_hash_mismatch",
                    path=str(self.weights_path),
                    expected=KNOWN_WEIGHTS_SHA256,
                    actual=self.weights_sha256,
                )
                return False
            log.info("yolo_weights_verified", sha256=self.weights_sha256)
        else:
            log.info("yolo_weights_hash", sha256=self.weights_sha256,
                     note="no ANPR_YOLO_SHA256 set; integrity check skipped")
        return True

    # -- model load ---------------------------------------------------------
    def _ensure_model(self) -> bool:
        if self._ml_ok is not None:
            return self._ml_ok
        if not self.verify_weights():
            self._ml_ok = False
            return False
        try:
            from ultralytics import YOLO  # lazy import by design

            self._model = YOLO(str(self.weights_path))
            self._ml_ok = True
            log.info("yolo_loaded", weights=str(self.weights_path))
        except Exception as exc:  # noqa: BLE001
            self._ml_ok = False
            log.warning("yolo_unavailable_fallback", error=str(exc))
        return self._ml_ok

    # -- detection ----------------------------------------------------------
    def detect(self, frame: Image) -> List[Detection]:
        """Return plate ROIs for a frame, highest-confidence first."""
        if not self._ensure_model():
            return self._fallback(frame)
        try:
            results = self._model.predict(frame, conf=self.conf, verbose=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("yolo_predict_failed_fallback", error=str(exc))
            return self._fallback(frame)

        dets: List[Detection] = []
        for res in results:
            boxes = getattr(res, "boxes", None)
            if boxes is None:
                continue
            for b in boxes:
                conf = float(b.conf[0]) if b.conf is not None else 0.0
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0].tolist())
                dets.append(Detection(bbox=(x1, y1, x2, y2), conf=conf))
        if not dets:
            return self._fallback(frame)
        dets.sort(key=lambda d: d.conf, reverse=True)
        return dets

    def crop(self, frame: Image, det: Detection) -> Image:
        x1, y1, x2, y2 = det.bbox
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        if x2 <= x1 or y2 <= y1:
            return frame
        return frame[y1:y2, x1:x2]

    # -- fallback -----------------------------------------------------------
    def _fallback(self, frame: Image) -> List[Detection]:
        """Classical plate finder: look for a bright, wide, high-contrast,
        plate-aspect rectangle in the lower half of the frame. Always returns at
        least one (degraded) candidate so the pipeline keeps producing output."""
        h, w = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        # Emphasise bright regions (plates are white) then find contours.
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best: Optional[Tuple[int, int, int, int]] = None
        best_area = 0
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if ch == 0:
                continue
            ar = cw / ch
            area = cw * ch
            # Plate-ish aspect (2:1 .. 6:1), reasonable size, lower 70% of frame.
            if 2.0 <= ar <= 6.0 and area > best_area and area > 0.01 * w * h and y > 0.2 * h:
                best, best_area = (x, y, x + cw, y + ch), area
        if best is not None:
            return [Detection(bbox=best, conf=0.30, degraded=True)]
        # Nothing plate-like — return the lower-centre band as a last resort.
        bx = (int(0.15 * w), int(0.5 * h), int(0.85 * w), h)
        return [Detection(bbox=bx, conf=0.0, degraded=True)]


__all__ = ["Detection", "PlateDetector", "sha256_file", "DEFAULT_WEIGHTS", "KNOWN_WEIGHTS_SHA256"]
