"""Tests for the identity face-quality + liveness gates.

The face-detection paths need OpenCV and real face fixtures, so they are validated
end-to-end against the running service; here we cover the deterministic, model-free
branches (missing/garbage image, liveness-without-model advisory) that must never
crash or hard-reject a real user.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from identity.quality import assess_quality, liveness_check  # noqa: E402


def test_quality_no_image():
    q = assess_quality(None)
    assert q["ok"] is False and q["reason"] == "no_image"


def test_quality_garbage_decode_fails_closed():
    try:
        import cv2  # noqa: F401
    except Exception:
        pytest.skip("opencv not installed (onnx extra)")
    q = assess_quality(b"this-is-not-a-jpeg")
    assert q["ok"] is False and q["reason"] == "decode_failed"


def test_liveness_without_model_is_advisory_and_never_blocks():
    # No image -> not checked, but must report live=True (never hard-reject).
    lv = liveness_check(None)
    assert lv["checked"] is False and lv["live"] is True
