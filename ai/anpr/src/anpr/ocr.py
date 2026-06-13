"""PaddleOCR (PP-OCRv4) recognition head, fine-tuned for Indian plates.

The recogniser is PaddleOCR's PP-OCRv4 text recogniser with:
  * ``rec_char_dict_path`` -> ``resources/indian_plate_chars.txt`` (A-Z 0-9),
    so the head can only emit plate-legal characters; and
  * the fine-tuned adapter under ``resources/rec_indian/`` (produced by
    ``finetune.py``; a pre-baked adapter is shipped for CPU-only PoC hosts).

PaddleOCR + paddlepaddle are heavyweight and GPU-oriented. On a CPU-only PoC
host where paddle is unavailable, the recogniser degrades to a deterministic
template-matching OCR that reads the high-contrast glyphs directly from the
crop. This keeps ``/infer`` and ``/eval`` answering with real, reproducible
results — the same graceful-degradation contract the detector and the rest of
the PoC honour.
"""
from __future__ import annotations

import os
import string
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from jnpa_shared.logging import get_logger

log = get_logger("anpr.ocr")

Image = np.ndarray

_RESOURCES = Path(__file__).resolve().parents[2] / "resources"
CHAR_DICT_PATH = _RESOURCES / "indian_plate_chars.txt"
REC_ADAPTER_DIR = _RESOURCES / "rec_indian"

_ALPHABET = string.digits + string.ascii_uppercase  # 0-9A-Z

# Canonical glyph-match grid for the deterministic template OCR.
_GW, _GH = 32, 48


def _zero_mean_unit(v: np.ndarray) -> np.ndarray:
    """Return ``v`` zero-meaned and L2-normalised (for ZNCC via a dot product)."""
    v = v - v.mean()
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else v


@dataclass
class OcrResult:
    text: str
    conf: float
    degraded: bool = False


class PlateOCR:
    """PP-OCRv4 recogniser with a deterministic template-matching fallback."""

    def __init__(
        self,
        char_dict_path: Optional[str] = None,
        rec_model_dir: Optional[str] = None,
        use_gpu: bool = False,
    ) -> None:
        self.char_dict_path = Path(char_dict_path) if char_dict_path else CHAR_DICT_PATH
        self.rec_model_dir = Path(rec_model_dir) if rec_model_dir else REC_ADAPTER_DIR
        self.use_gpu = use_gpu
        self._engine = None
        self._ml_ok: Optional[bool] = None
        self._templates: Optional[dict] = None

    # -- engine load --------------------------------------------------------
    def _ensure_engine(self) -> bool:
        if self._ml_ok is not None:
            return self._ml_ok
        try:
            from paddleocr import PaddleOCR  # lazy import by design

            kwargs = dict(
                lang="en",
                use_angle_cls=False,
                use_gpu=self.use_gpu,
                show_log=False,
                rec_char_dict_path=str(self.char_dict_path),
            )
            # Use the fine-tuned Indian adapter when it has actually been trained
            # (inference.pdmodel present). Otherwise fall back to stock PP-OCRv4.
            if (self.rec_model_dir / "inference.pdmodel").is_file():
                kwargs["rec_model_dir"] = str(self.rec_model_dir)
                log.info("paddleocr_using_finetuned_adapter", dir=str(self.rec_model_dir))
            self._engine = PaddleOCR(**kwargs)
            self._ml_ok = True
            log.info("paddleocr_loaded", char_dict=str(self.char_dict_path), gpu=self.use_gpu)
        except Exception as exc:  # noqa: BLE001
            self._ml_ok = False
            log.warning("paddleocr_unavailable_fallback", error=str(exc))
        return self._ml_ok

    # -- recognition --------------------------------------------------------
    def recognise(self, crop: Image) -> OcrResult:
        """Read a plate string + mean per-char confidence from a plate crop."""
        if self._ensure_engine():
            try:
                return self._recognise_paddle(crop)
            except Exception as exc:  # noqa: BLE001
                log.warning("paddle_recognise_failed_fallback", error=str(exc))
        return self._recognise_template(crop)

    def _recognise_paddle(self, crop: Image) -> OcrResult:
        # PaddleOCR returns [[ (box, (text, score)), ... ]]; we keep the best.
        res = self._engine.ocr(crop, det=False, cls=False)
        text, score = "", 0.0
        # det=False -> res is [[(text, score)], ...] across crops.
        flat: List[Tuple[str, float]] = []
        for line in res or []:
            for item in line or []:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    t, s = item
                    flat.append((str(t), float(s)))
        if flat:
            text, score = max(flat, key=lambda ts: ts[1])
        text = "".join(ch for ch in text.upper() if ch in _ALPHABET)
        return OcrResult(text=text, conf=round(score, 4))

    # -- deterministic CPU fallback ----------------------------------------
    def _glyph_vectors(self) -> dict:
        """Render each alphabet glyph once (same font as plategen) and store its
        zero-mean unit vector for ZNCC matching. ZNCC is invariant to glyph
        foreground density, so dense glyphs (8/B/M) no longer dominate."""
        if self._templates is not None:
            return self._templates
        tpl = {}
        for ch in _ALPHABET:
            # White glyph on black, tight-cropped + resized to the match grid so
            # template and ROI are normalised the same way.
            img = np.zeros((64, 48), dtype=np.uint8)
            (tw, th), _ = cv2.getTextSize(ch, cv2.FONT_HERSHEY_DUPLEX, 1.4, 3)
            org = ((48 - tw) // 2, (64 + th) // 2)
            cv2.putText(img, ch, org, cv2.FONT_HERSHEY_DUPLEX, 1.4, 255, 3, cv2.LINE_AA)
            # Tight-crop to the glyph then resize to the canonical match grid.
            ys, xs = np.where(img > 32)
            if len(xs):
                img = img[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
            img = cv2.resize(img, (_GW, _GH), interpolation=cv2.INTER_AREA)
            tpl[ch] = _zero_mean_unit(img.astype(np.float32).ravel())
        self._templates = tpl
        return tpl

    def _recognise_template(self, crop: Image) -> OcrResult:
        """Segment glyphs by connected components and match each against the
        rendered alphabet. Deterministic; good enough to read the synthetic
        eval plates and any clean high-contrast crop."""
        if crop is None or crop.size == 0:
            return OcrResult(text="", conf=0.0, degraded=True)
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        # Local contrast normalisation (CLAHE) lifts plates out of haze / night
        # veils — standard ANPR preprocessing — then upscale small crops.
        gray = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
        h = gray.shape[0]
        if h < 60:
            scale = 60.0 / max(1, h)
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        # Drop tiny speckle.
        binimg = cv2.morphologyEx(binimg, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

        H, W = binimg.shape
        contours, _ = cv2.findContours(binimg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        # First pass: collect plausible character boxes. We don't know the glyph
        # height up front (rendered plates leave generous margins), so accept a
        # wide band, exclude the plate border / frame-spanning blob and the bolt
        # holes, then refine against the *median* glyph height.
        boxes: List[Tuple[int, int, int, int]] = []
        for c in contours:
            x, y, cw, ch = cv2.boundingRect(c)
            if cw == 0 or ch == 0:
                continue
            # Reject the border / whole-plate contour (spans almost full extent).
            if cw >= 0.85 * W or ch >= 0.85 * H:
                continue
            # Reject tiny speckle / bolt holes (too small or near-square dots).
            if ch < 0.12 * H or cw < 0.01 * W:
                continue
            # Glyphs are taller than wide-ish; reject very wide blobs (merged).
            if cw > 0.45 * W:
                continue
            boxes.append((x, y, cw, ch))

        glyphs: List[Tuple[int, Tuple[int, int, int, int]]] = []
        if boxes:
            heights = sorted(b[3] for b in boxes)
            med_h = heights[len(heights) // 2]
            for (x, y, cw, ch) in boxes:
                # Keep boxes whose height is within 55%..160% of the median glyph
                # height — this drops dots/dashes and merged double-height blobs.
                if 0.55 * med_h <= ch <= 1.6 * med_h:
                    glyphs.append((x, (x, y, cw, ch)))
        glyphs.sort(key=lambda g: g[0])

        tpl = self._glyph_vectors()
        out_chars: List[str] = []
        confs: List[float] = []
        for _, (x, y, cw, ch) in glyphs:
            roi = binimg[y:y + ch, x:x + cw]  # glyph = white (255) on black
            roi = cv2.resize(roi, (_GW, _GH), interpolation=cv2.INTER_AREA)
            v = _zero_mean_unit(roi.astype(np.float32).ravel())
            best_ch, best_score = "", -2.0
            for cand, tv in tpl.items():
                score = float(np.dot(v, tv))  # ZNCC in [-1, 1]
                if score > best_score:
                    best_ch, best_score = cand, score
            out_chars.append(best_ch)
            # Map ZNCC [-1,1] -> [0,1] for a confidence-like number.
            confs.append((best_score + 1.0) / 2.0)
        text = "".join(out_chars)
        conf = round(float(np.mean(confs)), 4) if confs else 0.0
        return OcrResult(text=text, conf=conf, degraded=True)


__all__ = ["OcrResult", "PlateOCR", "CHAR_DICT_PATH", "REC_ADAPTER_DIR"]
