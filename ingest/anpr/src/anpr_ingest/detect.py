"""Vehicle detection + plate-candidate cropping.

Uses a YOLOv8n detector (ultralytics; weights downloaded on first run) to find
vehicles, then crops a plate-candidate region from the lower-centre of each
vehicle box. In DRY_RUN mode the raw crop is returned as the "result" (no call
to the AI ANPR service, which is built in Prompt 3.1). Otherwise the crop is
POSTed to ``ai_anpr_url`` and the OCR result is returned.

If ultralytics/torch is unavailable (or weights cannot load), the detector
degrades gracefully to a single full-frame candidate so the pipeline keeps
producing events — flagged ``degraded=True``.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import VehicleClass

from .config import AnprConfig

log = get_logger("anpr_ingest.detect")

Frame = np.ndarray

# COCO id -> our VehicleClass.
_COCO_TO_CLASS = {
    2: VehicleClass.CAR,
    3: VehicleClass.TWO_WHEELER,
    5: VehicleClass.BUS,
    7: VehicleClass.HGV,
}


@dataclass
class PlateCandidate:
    camera_id: str
    crop: Frame                     # BGR image of the plate-candidate region
    vehicle_class: VehicleClass
    det_conf: float                 # detector confidence for the vehicle box
    box: Tuple[int, int, int, int]  # x1,y1,x2,y2 of the vehicle in the frame
    degraded: bool = False

    def crop_b64_jpeg(self) -> str:
        ok, buf = cv2.imencode(".jpg", self.crop)
        if not ok:
            return ""
        return base64.b64encode(buf.tobytes()).decode("ascii")


class VehicleDetector:
    """Lazy-loading YOLOv8n vehicle detector with a no-ML fallback."""

    def __init__(self, cfg: AnprConfig) -> None:
        self.cfg = cfg
        self._model = None
        self._ml_ok: Optional[bool] = None  # tri-state: None=untried

    def _ensure_model(self) -> bool:
        if self._ml_ok is not None:
            return self._ml_ok
        try:
            from ultralytics import YOLO  # noqa: WPS433 (lazy import by design)

            self._model = YOLO(self.cfg.yolo_weights)
            self._ml_ok = True
            log.info("yolo_loaded", weights=self.cfg.yolo_weights)
        except Exception as exc:  # noqa: BLE001
            self._ml_ok = False
            log.warning("yolo_unavailable_fallback", error=str(exc))
        return self._ml_ok

    @staticmethod
    def _plate_region(frame: Frame, box: Tuple[int, int, int, int]) -> Frame:
        """Crop the lower-centre ~40% of a vehicle box where plates usually sit."""
        x1, y1, x2, y2 = box
        h = max(1, y2 - y1)
        w = max(1, x2 - x1)
        py1 = y1 + int(0.55 * h)
        px1 = x1 + int(0.20 * w)
        px2 = x2 - int(0.20 * w)
        py1 = min(py1, y2 - 1)
        px2 = max(px2, px1 + 1)
        crop = frame[py1:y2, px1:px2]
        if crop.size == 0:
            return frame[y1:y2, x1:x2] if (y2 > y1 and x2 > x1) else frame
        return crop

    def detect(self, camera_id: str, frame: Frame) -> List[PlateCandidate]:
        """Return plate candidates for a frame."""
        if not self._ensure_model():
            return self._fallback(camera_id, frame)

        candidates: List[PlateCandidate] = []
        try:
            results = self._model.predict(
                frame, conf=self.cfg.detect_conf, verbose=False
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("yolo_predict_failed_fallback", error=str(exc))
            return self._fallback(camera_id, frame)

        for res in results:
            boxes = getattr(res, "boxes", None)
            if boxes is None:
                continue
            for b in boxes:
                cls_id = int(b.cls[0]) if b.cls is not None else -1
                if cls_id not in self.cfg.vehicle_class_ids:
                    continue
                conf = float(b.conf[0]) if b.conf is not None else 0.0
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0].tolist())
                crop = self._plate_region(frame, (x1, y1, x2, y2))
                candidates.append(
                    PlateCandidate(
                        camera_id=camera_id,
                        crop=crop,
                        vehicle_class=_COCO_TO_CLASS.get(cls_id, VehicleClass.UNKNOWN),
                        det_conf=conf,
                        box=(x1, y1, x2, y2),
                    )
                )

        # If the model ran but found no vehicles (e.g. synthetic/empty footage),
        # optionally emit one degraded full-frame candidate so the pipeline still
        # produces snapshots. Real footage with real vehicles takes the path above.
        if not candidates and self.cfg.emit_on_empty:
            return self._fallback(camera_id, frame)
        return candidates

    def _fallback(self, camera_id: str, frame: Frame) -> List[PlateCandidate]:
        """No-ML path: treat the lower-centre band of the frame as one candidate."""
        h, w = frame.shape[:2]
        box = (0, 0, w, h)
        crop = self._plate_region(frame, box)
        return [
            PlateCandidate(
                camera_id=camera_id,
                crop=crop,
                vehicle_class=VehicleClass.UNKNOWN,
                det_conf=0.0,
                box=box,
                degraded=True,
            )
        ]
