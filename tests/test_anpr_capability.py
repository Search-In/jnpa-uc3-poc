"""ANPR capability envelope — the "do these numbers mean anything?" contract.

The 2026-08-04 audit found the Demo Console rendering **0.0% against a ≥95%
target**. The number was true and honestly plumbed, but the framing asserted
something false: that the bid stack had been measured and had failed. It had not
been measured at all — PaddleOCR was never loaded, because paddlepaddle 2.6
publishes no linux/arm64 wheel and ai/anpr/Dockerfile tolerates the failed
install ("will run degraded").

``describe_capability`` makes that distinction explicit and machine-readable so
the dashboard, the evidence pack and the presenter all tell the same story.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from anpr.evaluator import describe_capability

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ FULL stack
def test_full_stack_is_reportable():
    cap = describe_capability({"detector_ml": True, "ocr_ml": True, "degraded": False})
    assert cap["status"] == "FULL"
    assert cap["engine"] == "paddle+yolo"
    assert cap["missing"] == []
    assert cap["reason"] is None
    assert cap["remediation"] == []


# -------------------------------------------------------------- DEGRADED paths
def test_missing_recogniser_is_degraded_and_names_the_arm64_cause():
    """The real-world case on this host: detector loads, PaddleOCR does not."""
    cap = describe_capability({"detector_ml": True, "ocr_ml": False, "degraded": True})
    assert cap["status"] == "DEGRADED"
    assert cap["engine"] == "fallback"
    assert cap["missing"] == ["recogniser"]
    assert "template-matching fallback" in cap["reason"]
    # The remediation must name the ACTUAL fix. `download_anpr_weights.sh` (the
    # fix named in the original backlog) does not address a missing recogniser.
    remediation = " ".join(cap["remediation"])
    assert "arm64" in remediation
    assert "linux/amd64" in remediation


def test_missing_detector_points_at_the_weights_script():
    cap = describe_capability({"detector_ml": False, "ocr_ml": True, "degraded": True})
    assert cap["missing"] == ["detector"]
    assert "download_anpr_weights.sh" in " ".join(cap["remediation"])


def test_both_missing_reports_both():
    cap = describe_capability({"detector_ml": False, "ocr_ml": False, "degraded": True})
    assert cap["status"] == "DEGRADED"
    assert set(cap["missing"]) == {"detector", "recogniser"}
    assert len(cap["remediation"]) == 2


def test_degraded_reason_forbids_quoting_the_numbers():
    """The wording is load-bearing: it is what a presenter reads out."""
    cap = describe_capability({"detector_ml": True, "ocr_ml": False, "degraded": True})
    assert "must not be quoted as a result" in cap["reason"]
    assert "DEGRADED" in cap["headline"]


def test_absent_readiness_keys_are_treated_as_missing():
    """Fail closed: an unknown component is not assumed present."""
    cap = describe_capability({})
    assert cap["status"] == "DEGRADED"
    assert set(cap["missing"]) == {"detector", "recogniser"}


# ------------------------------------------------------- eval payload contract
def test_run_eval_payload_carries_the_envelope():
    """The /eval response must expose `capability` + `accuracy_reportable`.

    Asserted structurally against the source rather than by running the full eval
    (which renders 200 synthetic scenes and needs cv2 + the model stack).
    """
    src = (ROOT / "ai" / "anpr" / "src" / "anpr" / "evaluator.py").read_text()
    assert '"capability": capability' in src
    assert '"accuracy_reportable"' in src
    # Backward compatibility: the pre-existing keys must survive, since
    # tests/test_anpr_ai.py and the gateway normalizer both read them.
    for key in ('"OCR_TARGET_MET"', '"degraded"', '"engine"',
                '"combined_weighted_accuracy_pct"', '"target_pct"'):
        assert key in src, f"eval payload dropped {key} — that is a breaking change"


def test_gateway_passthrough_preserves_upstream_keys():
    """_normalize_anpr_eval must not filter `capability` out on its way to the UI."""
    src = (ROOT / "gateway" / "routers" / "anpr.py").read_text()
    assert "out = dict(data)" in src, (
        "the gateway normalizer must copy every upstream key, otherwise the "
        "capability envelope never reaches the dashboard"
    )


# ------------------------------------------------------------- evidence pack
def test_shipped_evidence_declares_reportability():
    """evidence/metrics.json must state whether its ocr_* figures are quotable.

    A 0.0 with no annotation is the exact ambiguity this closes.
    """
    path = ROOT / "evidence" / "metrics.json"
    if not path.exists():
        pytest.skip("evidence/metrics.json not built")
    ctx = json.loads(path.read_text()).get("_context", {})
    assert "ocr_accuracy_reportable" in ctx, (
        "evidence/metrics.json does not say whether its OCR accuracy may be "
        "quoted — re-run `make evidence`"
    )
    if ctx["ocr_accuracy_reportable"] is False:
        cap = ctx.get("ocr_capability") or {}
        assert cap.get("status") == "DEGRADED"
        assert cap.get("remediation"), "a degraded evidence pack must carry the fix"


def test_evidence_builder_records_the_envelope():
    src = (ROOT / "scripts" / "build_evidence.py").read_text()
    assert "ocr_accuracy_reportable" in src
    assert "ocr_capability" in src


# ------------------------------------------------------------------ dashboard
def test_demo_console_does_not_render_a_target_for_a_degraded_engine():
    """Regression guard for the finding itself.

    The Demo Console must branch on `accuracy_reportable` BEFORE rendering the
    accuracy-vs-target probe; otherwise it prints "0.0% / ≥95%" again.
    """
    src = (ROOT / "web" / "src" / "screens" / "DemoConsole.tsx").read_text()
    assert "accuracy_reportable === false" in src
    assert "DegradedProbe" in src


def test_model_performance_panel_strips_the_target_when_degraded():
    src = (ROOT / "web" / "src" / "components" / "panels" / "ModelPerformancePanel.tsx").read_text()
    assert "accuracy_reportable !== false" in src
    assert "reportable ? pct(e.target) : undefined" in src
