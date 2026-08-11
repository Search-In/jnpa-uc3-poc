"""UC3-023 — EC-6 camera-outage degraded gate ladder.

The mapping from a camera's cascade state to what the gate can honestly confirm
is pure, so it is tested without Redis, a frame bus or a server. The rule under
test is that losing evidence lowers the recorded confidence rather than being
hidden, and that replayed frames are never labelled LIVE.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from services.gate_board.service import (  # noqa: E402
    CARD_DOWN_VISIBLE_SECONDS,
    GATE_CONFIDENCE_BY_RUNG,
    NO_FEED_DETECT_SECONDS,
    SERVICE_RATE_FACTOR_BY_RUNG,
    GateBoardService,
    degraded_mode_for,
    rung_from_camera,
)


@pytest.mark.parametrize(
    "path,rung",
    [("LIVE", "LIVE"), ("CACHED", "DEGRADED"), ("SYNTHETIC", "NO_FEED"),
     ("", "NO_FEED"), (None, "NO_FEED")],
)
def test_rung_follows_the_cameras_own_cascade_state(path, rung):
    """The gate reads the ANPR cascade rather than inventing a parallel status."""
    assert rung_from_camera(path) == rung


def test_live_uses_the_anpr_rfid_join_at_full_confidence():
    m = degraded_mode_for({"camera_id": "CAM-1", "decision_path": "LIVE"})
    assert m["rung"] == "LIVE"
    assert m["confirmation_mode"] == "ANPR_RFID_JOIN"
    assert m["confidence"] == GATE_CONFIDENCE_BY_RUNG["LIVE"]
    assert m["manual_verify_lane"] is False
    assert m["source_card"] == "LIVE"
    assert m["feed_label"] == "LIVE"


def test_cached_frames_are_badged_replay_never_live():
    """Replayed footage presented as LIVE is the failure this badge prevents."""
    m = degraded_mode_for({"camera_id": "CAM-1", "decision_path": "CACHED"})
    assert m["feed_label"] == "REPLAY"
    assert m["feed_label"] != "LIVE"
    assert m["source_card"] == "DEGRADED"


def test_no_feed_falls_back_to_rfid_only_with_a_manual_lane():
    m = degraded_mode_for({"camera_id": "CAM-1", "decision_path": "SYNTHETIC"})
    assert m["no_feed"] is True
    assert m["confirmation_mode"] == "RFID_ONLY"
    assert m["manual_verify_lane"] is True
    assert m["source_card"] == "DOWN"
    assert m["feed_label"] == "NO FEED"
    assert "ANPR half of the join is unavailable" in m["confidence_basis"]


def test_confidence_falls_monotonically_as_evidence_is_lost():
    """A gate that kept full confidence after losing the camera would pass a
    false certainty to every downstream decision."""
    live = GATE_CONFIDENCE_BY_RUNG["LIVE"]
    degraded = GATE_CONFIDENCE_BY_RUNG["DEGRADED"]
    no_feed = GATE_CONFIDENCE_BY_RUNG["NO_FEED"]
    assert live > degraded > no_feed
    assert no_feed > 0, "an RFID-only read is still a read"


def test_service_rate_falls_so_the_queue_forecast_worsens():
    assert (SERVICE_RATE_FACTOR_BY_RUNG["LIVE"]
            > SERVICE_RATE_FACTOR_BY_RUNG["DEGRADED"]
            > SERVICE_RATE_FACTOR_BY_RUNG["NO_FEED"] > 0)


def test_ec6_timing_contract():
    assert NO_FEED_DETECT_SECONDS == 5
    assert CARD_DOWN_VISIBLE_SECONDS == 60


def test_fault_injection_is_reported_not_hidden():
    m = degraded_mode_for({"camera_id": "C", "decision_path": "SYNTHETIC", "forced": True})
    assert m["fault_injected"] is True


class _NoRepo:
    """The ladder needs no database; the service must not require one."""


@pytest.mark.asyncio
async def test_rollup_reports_overall_rung_and_effective_service_rate():
    svc = GateBoardService(repository=_NoRepo())
    cams = [
        {"camera_id": "A", "decision_path": "LIVE"},
        {"camera_id": "B", "decision_path": "CACHED"},
        {"camera_id": "C", "decision_path": "SYNTHETIC"},
    ]
    out = await svc.camera_degraded_mode(cams)
    assert out["count"] == 3
    assert out["degraded_count"] == 2      # CACHED + SYNTHETIC are both non-LIVE
    assert out["no_feed_count"] == 1
    assert out["overall_rung"] == "NO_FEED"  # worst rung wins
    assert out["effective_service_vph"] < out["nominal_service_vph"]
    assert out["timing_contract"]["no_feed_detect_seconds"] == 5


@pytest.mark.asyncio
async def test_all_live_reports_live_and_full_service_rate():
    svc = GateBoardService(repository=_NoRepo())
    out = await svc.camera_degraded_mode(
        [{"camera_id": "A", "decision_path": "LIVE"},
         {"camera_id": "B", "decision_path": "LIVE"}])
    assert out["overall_rung"] == "LIVE"
    assert out["service_rate_factor"] == 1.0
    assert out["effective_service_vph"] == out["nominal_service_vph"]


@pytest.mark.asyncio
async def test_no_cameras_reports_an_empty_ladder_not_a_false_live():
    svc = GateBoardService(repository=_NoRepo())
    out = await svc.camera_degraded_mode([])
    assert out["count"] == 0
    assert out["cameras"] == []


def test_restore_writes_a_reconciliation_to_the_decision_log():
    """Clearing a fault must leave a queryable trace, not just flip a flag."""
    src = (REPO_ROOT / "gateway" / "routers" / "control.py").read_text()
    assert "record_decision" in src
    assert '"RESTORED"' in src
    assert "FAULT_CLEARED" in src
    # `event` is structlog's reserved key; a detail key by that name raises.
    assert '"event": "FAULT_CLEARED"' not in src
