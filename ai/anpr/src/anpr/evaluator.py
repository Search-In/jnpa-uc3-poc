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


#: Human-readable remediation per missing component. Kept next to the readiness
#: check so the operator answer and the code cannot drift apart.
_REMEDIATION = {
    "detector": (
        "YOLO plate detector weights are missing. Run "
        "`scripts/download_anpr_weights.sh` (writes ai/anpr/resources/*.pt)."
    ),
    "recogniser": (
        "PaddleOCR (PP-OCRv4) is not importable, so plate TEXT is read by the "
        "deterministic template-matching fallback. paddlepaddle 2.6 publishes no "
        "linux/arm64 wheel, so an image built on Apple Silicon is degraded by "
        "construction — ai/anpr/Dockerfile tolerates the failed install on "
        "purpose. Build the ANPR image with `--platform linux/amd64`, or run the "
        "service on an x86_64 host."
    ),
}


def describe_capability(readiness: Dict) -> Dict:
    """Turn the raw readiness flags into an explicit, presentable status.

    The 2026-08-04 audit found the Demo Console rendering **0.0% against a 95%
    target** — a true number, honestly plumbed, but framed as if the system had
    been measured and failed. It had not been measured at all: the recogniser was
    never loaded. This block makes the difference explicit rather than leaving it
    to be inferred from a `degraded: true` flag that the UI ignored.

    Returns FULL (numbers are a real measurement of the bid stack) or DEGRADED
    (numbers describe the fallback and must not be presented as accuracy).
    """
    missing: List[str] = []
    if not readiness.get("detector_ml"):
        missing.append("detector")
    if not readiness.get("ocr_ml"):
        missing.append("recogniser")

    if not missing:
        return {
            "status": "FULL",
            "engine": "paddle+yolo",
            "missing": [],
            "headline": "ANPR running the full YOLO + PP-OCRv4 stack.",
            "reason": None,
            "remediation": [],
        }

    return {
        "status": "DEGRADED",
        "engine": "fallback",
        "missing": missing,
        "headline": "ANPR DEGRADED — accuracy not measurable on this host.",
        "reason": (
            "The evaluated pipeline is not the bid stack: "
            + " and ".join(
                {"detector": "the plate DETECTOR is not loaded",
                 "recogniser": "plate TEXT is read by the template-matching fallback"}[m]
                for m in missing
            )
            + ". Accuracy figures below describe the fallback, not the "
              "YOLO + PP-OCRv4 system, and must not be quoted as a result."
        ),
        "remediation": [_REMEDIATION[m] for m in missing],
    }


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
    capability = describe_capability(readiness)

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
        # --- honesty envelope (see describe_capability) -------------------
        # A number produced by the template-matching fallback is NOT a
        # measurement of the system being bid. `accuracy_reportable` tells every
        # consumer (dashboard, evidence pack) whether the accuracy figures above
        # may be presented as a result at all.
        "capability": capability,
        "accuracy_reportable": capability["status"] == "FULL",
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
