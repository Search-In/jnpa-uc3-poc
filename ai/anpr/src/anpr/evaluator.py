"""Held-out evaluation harness.

Builds a reproducible benchmark of real Indian plate strings, renders each into
a synthetic camera scene (``plategen``), applies the three condition slices
(clean / dust+haze / night) via ``degradation``, runs the full pipeline, and
scores OCR character accuracy + exact-match per slice.

Ground-truth plates are sourced, in order of preference:
  1. ``data/fixtures/known_plates.json`` (the deterministic Vahan dataset — real
     well-formed plates shared across the PoC), held-out 15% tail split; else
  2. a deterministic generated set of valid plates.

This is what ``/eval`` and ``eval/bench.py`` both call.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from jnpa_shared.logging import get_logger

from .config import AnprAiConfig
from .degradation import DEGRADATIONS
from .metrics import (
    SliceMetrics,
    combined_weighted_accuracy,
    score_slice,
)
from .pipeline import AnprPipeline
from .plategen import render_scene

log = get_logger("anpr.eval")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_FIXTURES = _REPO_ROOT / "data" / "fixtures" / "known_plates.json"

_STATES = ["MH", "GJ", "KA", "DL", "TN", "UP", "RJ", "KL", "WB", "AP"]
_SERIES = ["AA", "AB", "BC", "CD", "AAA", "ZZ"]


def _generated_plates(n: int) -> List[str]:
    """Deterministic valid classic plates (no RNG / wall-clock)."""
    plates: List[str] = []
    i = 0
    while len(plates) < n:
        st = _STATES[i % len(_STATES)]
        dist = (i % 99) + 1
        ser = _SERIES[i % len(_SERIES)]
        num = 1000 + (i * 37) % 9000
        plates.append(f"{st}{dist:02d}{ser}{num:04d}")
        i += 1
    return plates


def load_plates(n: int) -> Tuple[List[str], str]:
    """Return (plates, source). Uses the fixtures' held-out 15% tail if present."""
    try:
        data = json.loads(_FIXTURES.read_text(encoding="utf-8"))
        all_plates = [
            p["plate"]
            for p in data.get("plates", [])
            if isinstance(p, dict) and p.get("plate")
        ]
        # Held-out 15% tail split (the head is "training"; we eval on the tail).
        if all_plates:
            cut = max(1, int(len(all_plates) * 0.85))
            held = all_plates[cut:]
            if len(held) < n:
                # Top up deterministically without overlapping the held set.
                extra = [p for p in _generated_plates(n * 2) if p not in set(all_plates)]
                held = (held + extra)[:n]
            else:
                held = held[:n]
            return held, "fixtures_holdout"
    except (OSError, ValueError, KeyError):
        pass
    return _generated_plates(n), "generated"


def run_eval(
    pipeline: AnprPipeline,
    cfg: AnprAiConfig,
    n: Optional[int] = None,
) -> Dict:
    """Run all three slices and return a metrics dict (the /eval payload)."""
    n = n or cfg.eval_set_size
    plates, source = load_plates(n)
    n = len(plates)
    # Surface which engine actually ran so the reported numbers are never
    # misread: the bid stack (paddle + YOLO weights) hits >=95%; the CPU
    # fallback reports its real, lower accuracy honestly.
    readiness = pipeline.warm()
    engine = "paddle+yolo" if not readiness["degraded"] else "fallback"
    log.info("eval_start", n=n, source=source, engine=engine, **readiness)

    # Render the clean scenes once; degradations are applied per slice.
    scenes: List[Tuple[str, np.ndarray, Tuple[int, int, int, int]]] = []
    for i, plate in enumerate(plates):
        frame, bbox = render_scene(plate, seed=1337 + i)
        scenes.append((plate, frame, bbox))

    slice_metrics: List[SliceMetrics] = []
    detection_stats: Dict[str, Dict] = {}

    for slice_name, degrade in DEGRADATIONS.items():
        preds: List[str] = []
        truths: List[str] = []
        det_hits = 0
        det_iou_sum = 0.0
        for i, (plate, frame, bbox) in enumerate(scenes):
            img = degrade(frame, seed=4242 + i)
            res = pipeline.infer(img)
            preds.append(res.plate)
            truths.append(plate)
            # Detection scoring: IoU of predicted bbox vs known plate bbox.
            if res.bbox is not None:
                iou = _iou(res.bbox, bbox)
                det_iou_sum += iou
                if iou >= 0.3:
                    det_hits += 1
        sm = score_slice(slice_name, preds, truths)
        slice_metrics.append(sm)
        detection_stats[slice_name] = {
            "detection_recall@0.3iou": round(det_hits / n, 4) if n else 0.0,
            "mean_iou": round(det_iou_sum / n, 4) if n else 0.0,
        }
        log.info(
            "eval_slice_done",
            slice=slice_name,
            exact_match=round(sm.exact_match, 4),
            cer=round(sm.mean_cer, 4),
        )

    combined = combined_weighted_accuracy(slice_metrics)
    target_met = combined >= cfg.eval_target_pct

    # Per-slice gate checks from the spec.
    by_name = {sm.name: sm for sm in slice_metrics}
    gates = {
        "clean_exact>=0.95": by_name["clean"].exact_match >= 0.95,
        "clean_char_acc>=0.97": by_name["clean"].char_accuracy >= 0.97,
        "dust_haze_exact>=0.92": by_name["dust_haze"].exact_match >= 0.92,
        "night_exact>=0.90": by_name["night"].exact_match >= 0.90,
    }

    return {
        "n": n,
        "source": source,
        "engine": engine,
        "weights_sha256": readiness.get("weights_sha256"),
        "degraded": readiness["degraded"],
        "slices": [sm.as_dict() for sm in slice_metrics],
        "detection": detection_stats,
        "combined_weighted_accuracy_pct": round(combined, 2),
        "target_pct": cfg.eval_target_pct,
        "gates": gates,
        "OCR_TARGET_MET": target_met,
    }


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


__all__ = ["run_eval", "load_plates"]
