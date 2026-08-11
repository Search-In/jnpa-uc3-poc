"""UC3-021 Gate & Lane Board + UC3-027 CPP metered release.

Pure-function tests of the two rules that these tickets are actually judged on,
so they hold without a database or a running server:

  * UI-068 — the queue is COUNTED, never inferred from throughput. The spec test
    is a trick question: stop a gate and the queue must RISE while throughput
    reads zero. A throughput-derived queue fails that by construction.
  * F-06 / UI-111 — only the congested terminal's release slows, and the UNIFORM
    arm visibly degrades the outcome for that terminal.
  * UI-103 — applying a lane reassignment produces a human task, never an
    equipment command.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from services.gate_board.repository import ACCEPTED_QUEUE_METHODS, GATE_TERMINAL  # noqa: E402
from services.gate_board.service import (  # noqa: E402
    QUEUE_HIGH,
    QUEUE_MEDIUM,
    QUEUE_TARGET,
    GateBoardService,
    advice_text,
    congestion_level,
    release_plan_for,
)


# --------------------------------------------------------------- UC3-021
def test_throughput_derived_is_not_an_accepted_queue_method():
    """The whole UI-068 guarantee: throughput may not be a counting method.

    Migration 0136 enforces this with a foreign key to core.queue_count_method;
    this asserts the service agrees, so the two cannot drift.
    """
    assert "THROUGHPUT_DERIVED" not in ACCEPTED_QUEUE_METHODS
    assert "VIDEO_ANALYTICS" in ACCEPTED_QUEUE_METHODS


def test_congestion_thresholds_are_8_and_20():
    assert QUEUE_MEDIUM == 8
    assert QUEUE_HIGH == 20
    assert congestion_level(0) == "LOW"
    assert congestion_level(7) == "LOW"
    assert congestion_level(8) == "MEDIUM"
    assert congestion_level(19) == "MEDIUM"
    assert congestion_level(20) == "HIGH"
    assert congestion_level(41) == "HIGH"


def test_no_camera_observation_yields_no_queue_not_a_zero():
    """An unobserved gate must be distinguishable from an empty one."""
    assert congestion_level(None) is None


class _FakeRepo:
    """Repository stub: the two reads the board makes, independently controlled.

    Being able to set the counted queue and the throughput SEPARATELY is the
    point — it is what lets the gate-stop test be written at all.
    """

    def __init__(self, queues, flows, gates=None, lanes=None):
        self._queues, self._flows = queues, flows
        self._gates = gates or [
            {"id": g, "name": g, "lat": 0.0, "lon": 0.0, "closed_at": None}
            for g in GATE_TERMINAL
        ]
        self._lanes = lanes or []

    async def gates(self):
        return self._gates

    async def queue_at_gates(self):
        return self._queues

    async def throughput_at_gates(self, window_minutes=60):
        return self._flows

    async def lanes(self, gate_id=None):
        return [l for l in self._lanes if not gate_id or l["gate_id"] == gate_id]

    async def lane(self, lane_id):
        return next((l for l in self._lanes if l["lane_id"] == lane_id), None)

    async def create_reassignment_task(self, **kw):
        # Mirrors the DB default + CHECK: the column can only ever be false.
        return {"task_id": "t-1", "status": "PENDING",
                "dispatched_to_equipment": False, **kw}

    async def record_release_plans(self, plans):
        return list(plans)

    async def latest_release_plans(self, mode="METERED"):
        return []


def _queue_row(gate_id, n):
    return {"gate_id": gate_id, "camera_id": f"CAM-{gate_id}-1", "queue_count": n,
            "vehicle_count": n, "congestion_level": None, "confidence": 0.9,
            "count_method": "VIDEO_ANALYTICS", "source": "CAMERA_AI",
            "observed_at": None}


def _flow_row(gate_id, in_count, out_count=0, avg_s=120.0):
    return {"gate_id": gate_id, "in_count": in_count, "out_count": out_count,
            "avg_txn_seconds": avg_s, "txn_samples": in_count}


@pytest.mark.asyncio
async def test_stopped_gate_queue_rises_while_throughput_reads_zero():
    """UI-068's spec test, executed.

    G-NSICT is stopped: zero in/out events. Its camera keeps counting a growing
    queue. The board must report BOTH — a rising queue and zero throughput. A
    queue derived from throughput would report 0 here, which is the failure this
    test exists to catch.
    """
    svc = GateBoardService(repository=_FakeRepo(
        queues=[_queue_row("G-NSICT", 34), _queue_row("G-BMCT", 3)],
        flows=[_flow_row("G-NSICT", 0, 0, None), _flow_row("G-BMCT", 30, 25)],
    ))
    out = await svc.gate_cards()
    cards = {c["gate_id"]: c for c in out["gates"]}

    stopped = cards["G-NSICT"]
    assert stopped["throughput_60min"] == 0, "the gate is stopped"
    assert stopped["queue_vehicles"] == 34, "the counted queue must survive the stop"
    assert stopped["queue_status"] == "COUNTED"
    assert stopped["congestion_level"] == "HIGH"
    assert stopped["queue_count_method"] == "VIDEO_ANALYTICS"

    # And the board says, in the payload itself, that it did not infer it.
    assert out["queue_provenance"]["derived_from_throughput"] is False


@pytest.mark.asyncio
async def test_gate_without_camera_reports_no_observation_not_zero():
    svc = GateBoardService(repository=_FakeRepo(
        queues=[_queue_row("G-NSICT", 12)],
        flows=[_flow_row("G-NSICT", 20), _flow_row("G-BMCT", 18)],
    ))
    cards = {c["gate_id"]: c for c in (await svc.gate_cards())["gates"]}
    assert cards["G-BMCT"]["queue_vehicles"] is None
    assert cards["G-BMCT"]["queue_status"] == "NO_OBSERVATION"
    assert cards["G-BMCT"]["congestion_level"] is None
    # Throughput is still reported — the gap is in the queue only.
    assert cards["G-BMCT"]["throughput_60min"] == 18


@pytest.mark.asyncio
async def test_reassignment_creates_a_human_task_and_no_equipment_command():
    """UI-103: applying must produce a workflow task, never an actuation."""
    lanes = [{"lane_id": "G-NSICT-L3", "gate_id": "G-NSICT", "lane_no": 3,
              "lane_type": "REVERSIBLE", "lane_state": "OPEN",
              "boom_barrier": "DOWN", "updated_at": None}]
    svc = GateBoardService(repository=_FakeRepo(
        queues=[_queue_row("G-NSICT", 30)],
        flows=[_flow_row("G-NSICT", 20)],
        lanes=lanes,
    ))

    preview = await svc.preview_reassignment(lane_id="G-NSICT-L3", to_type="IN")
    assert preview["sends_equipment_command"] is False
    assert preview["applies_as"] == "HUMAN_TASK"
    assert preview["simulated"] is True

    applied = await svc.apply_reassignment(lane_id="G-NSICT-L3", to_type="IN",
                                           reason="test", actor="tester")
    assert applied["sends_equipment_command"] is False
    assert applied["lane_state_changed"] is False
    assert applied["task"]["dispatched_to_equipment"] is False
    assert applied["task"]["status"] == "PENDING"


# --------------------------------------------------------------- UC3-027
def test_metered_release_slows_only_the_congested_terminal():
    """F-06: one terminal's congestion must not slow another's release."""
    congested = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                                 queue=40, clearing_rate_vph=12.0)
    calm = release_plan_for(terminal_code="BMCT", gate_id="G-BMCT",
                            queue=3, clearing_rate_vph=12.0)

    assert congested["release_rate_vph"] < congested["clearing_rate_vph"]
    assert calm["release_rate_vph"] == calm["clearing_rate_vph"], (
        "an uncongested terminal must release at its full clearing rate")
    assert congested["hold_minutes"] > 0
    assert calm["hold_minutes"] == 0


def test_release_rate_is_never_zero_only_metered():
    """A terminal is throttled, never stopped: a hard 0 strands trucks."""
    plan = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                            queue=5_000, clearing_rate_vph=12.0)
    assert plan["release_rate_vph"] > 0


def test_uniform_mode_degrades_the_congested_terminal():
    """UI-111: the do-nothing comparison gives the congested terminal no relief."""
    metered = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                               queue=40, clearing_rate_vph=12.0, mode="METERED")
    uniform = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                               queue=40, clearing_rate_vph=12.0, mode="UNIFORM",
                               uniform_rate_vph=12.0)
    assert uniform["release_rate_vph"] > metered["release_rate_vph"], (
        "UNIFORM keeps feeding the congested gate — that is the point of the "
        "comparison")
    assert uniform["mode"] == "UNIFORM"


def test_advice_sentence_matches_the_ui_156_format():
    """UI-156 format: 'Hold at plaza for about N minutes - the gate queue is Q
    vehicles and clearing at R per hour.'"""
    text = advice_text(25, 40, 12.0)
    assert text == (
        "Hold at plaza for about 25 minutes - the gate queue is 40 vehicles "
        "and clearing at 12 per hour.")


def test_advice_says_proceed_when_the_queue_is_under_target():
    text = advice_text(0, 3, 14.0)
    assert text.startswith("Proceed to the gate")
    assert "3 vehicles" in text


def test_every_advice_number_appears_in_the_plan_that_produced_it():
    """The driver sentence and the control-room row must share one arithmetic."""
    plan = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                            queue=40, clearing_rate_vph=12.0)
    assert str(plan["gate_queue_vehicles"]) in plan["advice_text"]
    assert str(round(plan["clearing_rate_vph"])) in plan["advice_text"]
    assert str(plan["hold_minutes"]) in plan["advice_text"]


def test_release_plans_are_tagged_simulated_with_a_disclosed_method():
    plan = release_plan_for(terminal_code="NSICT", gate_id="G-NSICT",
                            queue=40, clearing_rate_vph=12.0)
    assert plan["simulated"] is True
    assert plan["method"]["queue_target_vehicles"] == QUEUE_TARGET
    assert "clearing_rate" in plan["method"]["release_rate_formula"]
    assert "core.camera_ai_count" in plan["method"]["queue_source"]


@pytest.mark.asyncio
async def test_a_terminal_with_no_counted_queue_gets_no_release_plan():
    """No counted queue means no invented release rate and no invented advice."""
    svc = GateBoardService(repository=_FakeRepo(
        queues=[_queue_row("G-NSICT", 30)],
        flows=[_flow_row("G-NSICT", 12), _flow_row("G-BMCT", 20)],
    ))
    out = await svc.compute_release_plans(persist=False)
    terminals = {p["terminal_code"] for p in out["plans"]}
    assert terminals == {"NSICT"}, "only the observed terminal is planned for"
