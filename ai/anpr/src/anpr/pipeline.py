"""End-to-end ANPR pipeline: detect plate ROI -> OCR -> post-process.

A single :class:`AnprPipeline` instance is created at service startup and shared
by every request (the models are lazy-loaded on first use). ``infer`` takes a
full BGR frame, ``infer_crop`` takes an already-cropped plate (the ingest
service POSTs crops, not whole frames).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

from jnpa_shared.logging import get_logger

from .config import AnprAiConfig
from .detect import Detection, PlateDetector
from .ocr import PlateOCR
from .postprocess import PlateResult, postprocess

log = get_logger("anpr.pipeline")

Image = np.ndarray


@dataclass
class InferResult:
    plate: str
    conf: float
    bbox: Optional[Tuple[int, int, int, int]]
    valid: bool
    series: Optional[str]
    raw_ocr: str
    fixes: list
    degraded: bool

    def as_dict(self) -> dict:
        return {
            "plate": self.plate,
            "conf": round(self.conf, 4),
            "bbox": list(self.bbox) if self.bbox else None,
            "valid": self.valid,
            "series": self.series,
            "raw_ocr": self.raw_ocr,
            "fixes": self.fixes,
            "degraded": self.degraded,
        }


class AnprPipeline:
    def __init__(self, cfg: AnprAiConfig) -> None:
        self.cfg = cfg
        self.detector = PlateDetector(weights=cfg.yolo_weights, conf=cfg.detect_conf)
        self.ocr = PlateOCR(
            char_dict_path=cfg.char_dict_path,
            rec_model_dir=cfg.rec_model_dir,
            use_gpu=cfg.use_gpu,
        )

    def warm(self) -> dict:
        """Force model load + weights hash-verify; return a readiness summary."""
        weights_ok = self.detector.verify_weights()
        ml_detector = self.detector._ensure_model()
        ml_ocr = self.ocr._ensure_engine()
        return {
            "weights_present": weights_ok,
            "weights_sha256": self.detector.weights_sha256,
            "detector_ml": ml_detector,
            "ocr_ml": ml_ocr,
            "degraded": not (ml_detector and ml_ocr),
        }

    # -- inference ----------------------------------------------------------
    def infer_crop(self, crop: Image, bbox: Optional[Tuple[int, int, int, int]] = None) -> InferResult:
        """OCR + post-process an already-cropped plate image."""
        ocr_res = self.ocr.recognise(crop)
        pp: PlateResult = postprocess(ocr_res.text)
        plate = pp.plate or ocr_res.text
        return InferResult(
            plate=plate,
            conf=ocr_res.conf,
            bbox=bbox,
            valid=pp.valid,
            series=pp.series,
            raw_ocr=ocr_res.text,
            fixes=pp.fixes,
            degraded=ocr_res.degraded,
        )

    def infer(self, frame: Image) -> InferResult:
        """Detect the best plate ROI in a frame, then OCR + post-process it."""
        dets = self.detector.detect(frame)
        det: Detection = dets[0]
        crop = self.detector.crop(frame, det)
        res = self.infer_crop(crop, bbox=det.bbox)
        # Detector confidence folds into the reported confidence when present.
        if not det.degraded and det.conf > 0:
            res.conf = round(0.5 * res.conf + 0.5 * det.conf, 4)
        res.degraded = res.degraded or det.degraded
        return res


__all__ = ["AnprPipeline", "InferResult"]
