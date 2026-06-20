"""SMS advisory seam tests (Wave 4 — APP-3 / SCOPE-IU2).

Proves the provider seam exists and is exercised:
  * default provider is the no-op (no credentials, sends nothing, never raises),
  * the "log" provider reports delivered,
  * advisory_to_sms_text renders a concise body,
  * send_sms is resilient (no number -> not delivered, never raises).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from gateway import sms  # noqa: E402


def test_default_provider_is_noop():
    sms.reset_provider()
    os.environ.pop("SMS_PROVIDER", None)
    p = sms.get_provider()
    assert p.name == "none"
    r = p.send("+919999999999", "hello")
    assert r.delivered is False and r.provider == "none"


def test_log_provider_reports_delivered():
    os.environ["SMS_PROVIDER"] = "log"
    sms.reset_provider()
    try:
        r = sms.send_sms("+919999999999", "reroute to G-BMCT")
        assert r.delivered is True and r.provider == "log"
    finally:
        os.environ.pop("SMS_PROVIDER", None)
        sms.reset_provider()


def test_send_sms_no_number_is_not_delivered_and_safe():
    sms.reset_provider()
    r = sms.send_sms("", "x")
    assert r.delivered is False  # and did not raise


def test_advisory_to_sms_text_is_concise():
    text = sms.advisory_to_sms_text(
        {"title": "Re-route advisory", "message": "Gate closed", "gate_id": "G-BMCT", "eta_min": 12}
    )
    assert "Re-route advisory" in text
    assert "G-BMCT" in text
    assert "12" in text
    assert len(text) <= 320
