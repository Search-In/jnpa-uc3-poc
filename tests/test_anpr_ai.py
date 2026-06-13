"""Unit tests for the ANPR + OCR inference service (ai/anpr).

These cover the deterministic, ML-free parts of the pipeline that the bid's
accuracy claim rests on:

  * the post-processor (regex grammars, state-code whitelist, confusion fixer),
  * the OCR-accuracy metrics (CER/WER/exact-match/combined gate),
  * the degradation augmenter (shape/dtype invariants),
  * the synthetic plate generator (reproducibility + known bbox), and
  * the end-to-end pipeline on a clean rendered plate (fallback path).

They run without paddle / YOLO weights / the docker stack, so `make test`
stays green on a CPU host. The full >=95% accuracy gate is exercised by
`ai/anpr/eval/bench.py` against the real stack.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ANPR_AI_SRC = REPO_ROOT / "ai" / "anpr" / "src"
SHARED_DIR = REPO_ROOT / "shared"
for p in (str(ANPR_AI_SRC), str(SHARED_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


# --------------------------------------------------------------------------
# Post-processor
# --------------------------------------------------------------------------
from anpr.postprocess import (  # noqa: E402
    DIGIT_TO_LETTER,
    LETTER_TO_DIGIT,
    STATE_CODES,
    postprocess,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("MH04AB1234", "MH04AB1234"),
        ("mh04ab1234", "MH04AB1234"),
        ("MH-04-AB-1234", "MH04AB1234"),
        ("MH 04 AB 1234", "MH04AB1234"),
        ("GJ01AAA1234", "GJ01AAA1234"),
        ("MH43A1234", "MH43A1234"),
    ],
)
def test_classic_plate_valid(raw, expected):
    res = postprocess(raw)
    assert res.plate == expected
    assert res.valid is True
    assert res.series == "classic"
    assert res.state in STATE_CODES


def test_bh_series_valid():
    res = postprocess("21BH0008BV")
    assert res.plate == "21BH0008BV"
    assert res.valid is True
    assert res.series == "bh"


def test_confusion_fix_only_on_digit_positions():
    # The trailing 4 chars MUST be digits: O->0, I->1, S->5, B->8, Z->2.
    res = postprocess("MH04ABI2S4")  # I->1, S->5 in the digit block
    assert res.plate == "MH04AB1254"
    assert res.valid is True
    assert any("I->1" in f for f in res.fixes)
    assert any("S->5" in f for f in res.fixes)


def test_confusion_fix_letter_position():
    # State prefix MUST be letters: a 0 read here should become O.
    res = postprocess("0H04AB1234")  # leading 0 in a letter slot -> O
    assert res.plate == "OH04AB1234"
    # OH is not a real state code, so validity is False but the fix still applied.
    assert any("0->O" in f for f in res.fixes)


def test_confusion_tables_are_inverse():
    for k, v in LETTER_TO_DIGIT.items():
        assert DIGIT_TO_LETTER[v] == k


def test_unknown_garbage_not_valid():
    res = postprocess("!!!")
    assert res.valid is False
    assert res.plate == ""


def test_state_whitelist_rejected():
    # ZZ is not a real RTO code; grammar matches but whitelist rejects.
    res = postprocess("ZZ04AB1234")
    assert res.series == "classic"
    assert res.valid is False


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
from anpr.metrics import (  # noqa: E402
    char_error_rate,
    combined_weighted_accuracy,
    levenshtein,
    score_slice,
)


def test_levenshtein_basic():
    assert levenshtein("MH04AB1234", "MH04AB1234") == 0
    assert levenshtein("MH04AB1234", "MH04AB1235") == 1
    assert levenshtein("", "ABC") == 3


def test_cer():
    assert char_error_rate("MH04AB1234", "MH04AB1234") == 0.0
    assert char_error_rate("MH04AB1235", "MH04AB1234") == pytest.approx(0.1)


def test_score_slice_exact():
    sm = score_slice("clean", ["MH04AB1234", "GJ01AA0001"], ["MH04AB1234", "GJ01AA0001"])
    assert sm.exact_match == 1.0
    assert sm.mean_cer == 0.0
    assert sm.char_accuracy == 1.0


def test_combined_weighted_accuracy_gate():
    slices = [
        score_slice("clean", ["A"], ["A"]),       # 100%
        score_slice("dust_haze", ["A"], ["A"]),   # 100%
        score_slice("night", ["A"], ["A"]),       # 100%
    ]
    assert combined_weighted_accuracy(slices) == pytest.approx(100.0)


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------
from anpr.degradation import DEGRADATIONS, dust_haze, low_light, night_low_light  # noqa: E402


def test_degradations_preserve_shape_and_dtype():
    img = (np.ones((100, 320, 3)) * 200).astype(np.uint8)
    for name, fn in DEGRADATIONS.items():
        out = fn(img, seed=1)
        assert out.shape == img.shape, name
        assert out.dtype == np.uint8, name


def test_low_light_darkens():
    img = (np.ones((50, 50, 3)) * 200).astype(np.uint8)
    out = low_light(img, gamma=2.5, noise_sigma=0.0, seed=0)
    assert out.mean() < img.mean()


def test_degradation_is_deterministic():
    img = (np.ones((100, 320, 3)) * 180).astype(np.uint8)
    a = dust_haze(img, seed=7)
    b = dust_haze(img, seed=7)
    assert np.array_equal(a, b)


# --------------------------------------------------------------------------
# Plate generator
# --------------------------------------------------------------------------
from anpr.plategen import render_plate, render_scene  # noqa: E402


def test_render_plate_deterministic():
    a = render_plate("MH04AB1234", seed=3)
    b = render_plate("MH04AB1234", seed=3)
    assert np.array_equal(a, b)


def test_render_scene_bbox_within_frame():
    frame, bbox = render_scene("MH04AB1234", seed=5)
    x1, y1, x2, y2 = bbox
    H, W = frame.shape[:2]
    assert 0 <= x1 < x2 <= W
    assert 0 <= y1 < y2 <= H


# --------------------------------------------------------------------------
# End-to-end pipeline (fallback path, no paddle/weights required)
# --------------------------------------------------------------------------
from anpr.config import AnprAiConfig  # noqa: E402
from anpr.pipeline import AnprPipeline  # noqa: E402


def test_pipeline_reads_clean_plate():
    """On a clean rendered plate the fallback pipeline should recover most chars
    and produce a grammar-valid plate after post-processing."""
    cfg = AnprAiConfig()
    pipeline = AnprPipeline(cfg)
    frame, _ = render_scene("MH04AB1234", seed=1)
    res = pipeline.infer(frame)
    assert isinstance(res.plate, str)
    assert res.bbox is not None
    # Character accuracy on a clean plate should be high even in fallback mode.
    cer = char_error_rate(res.plate, "MH04AB1234")
    assert cer <= 0.4, f"clean-plate CER too high: {cer} (got {res.plate!r})"
