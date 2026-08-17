"""End-to-end tests for the what-if scenarios (Sub-Criterion 5).

Two layers:

* Pure-unit (always run): the UC-II->UC-III demand translation and the scenario
  registry — no stack needed.
* End-to-end (skipped unless the scenarios-runner is reachable on host :8400):
  run each scenario, assert all 5 dashboard steps fire, and reset cleanly. These
  mirror the bid's "each scenario is wrapped in a pytest that runs it end-to-end,
  asserts all 5 dashboard steps fire, and resets cleanly."

Run the e2e layer with the stack up:  make up && pytest tests/test_scenarios.py
"""
from __future__ import annotations

import socket
import time
from typing import Optional

import pytest


# --------------------------------------------------------------------------- unit
def test_uc2_bridge_matches_spec():
    """2.5x DPD release -> 600 trucks/h over 40 min (the TFC-3 figures)."""
    from scenarios.uc2_bridge import translate_release

    p = translate_release({"dpd_release_spike": 2.5, "window_min": 40})
    assert p.trucks_per_h == 600
    assert p.window_min == 40
    assert p.total_trucks == 400


def test_registry_has_expected_scenarios():
    from scenarios import get_scenario, scenario_names

    assert set(scenario_names()) == {"tfc1", "tfc2", "tfc3", "tfc4", "monsoon_friday"}
    # The master scenario exposes the run/reset contract like the others.
    mf = get_scenario("monsoon_friday")
    assert mf is not None and callable(mf.run) and callable(mf.reset)
    # TFC-4 (UC-3 peak yard / truck arrival management) is registered on the same
    # contract — the What-If Console runs it through the identical run/reset path.
    t4 = get_scenario("tfc4")
    assert t4 is not None and callable(t4.run) and callable(t4.reset)


def test_stub_cleanup_tags_match_run_tags():
    """Post-restart stub resets must mint the SAME tags run() uses — the old
    generic ``{NAME.upper()}:{id}`` stub silently removed zero trucks."""
    from scenarios import tfc1, tfc2, tfc3, tfc4, monsoon_friday

    hid = "sc_test123"
    assert tfc1.stub_cleanup(hid)["truck_tag"] == f"TFC-1:{hid}"
    assert tfc3.stub_cleanup(hid)["truck_tag"] == f"TFC-3:{hid}"
    m = monsoon_friday.stub_cleanup(hid)
    assert m["demand_tag"] == f"MONSOON:demand:{hid}"
    assert m["queue_tag"] == f"MONSOON:queue:{hid}"
    assert tfc2.stub_cleanup(hid)["device_id"] == f"SYN-TFC2-{hid}"
    assert tfc4.stub_cleanup(hid)["truck_tag"] == f"TFC-4:{hid}"


def _kafka_up() -> bool:
    # 29092 is the EXTERNAL (host-reachable) listener; 9092 is INTERNAL and
    # advertises the docker-network name `kafka:9092` which won't resolve here.
    try:
        with socket.create_connection(("localhost", 29092), timeout=2.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _kafka_up(), reason="Kafka not reachable on localhost:29092")
def test_uc2_bridge_consumes_from_broker():
    """publish -> Kafka -> uc2_bridge listener -> next_event round-trip.

    Proves the cross-twin consumption is REAL (not the inline fallback):
    the event must come back through the broker via group uc3-uc2-bridge.
    """
    import asyncio
    import os

    # Host-side run: point jnpa_shared at the EXTERNAL listener (containers use
    # the in-network default kafka:9092). Settings are built per-call, so this
    # takes effect without a reload.
    os.environ.setdefault("KAFKA_BROKERS", "localhost:29092")

    from jnpa_shared import kafka_io
    from scenarios import uc2_bridge

    async def _roundtrip() -> dict:
        await uc2_bridge.start_listener(group=f"uc3-uc2-bridge-test-{int(time.time())}")
        try:
            assert await asyncio.to_thread(uc2_bridge.wait_assigned, 30.0), \
                "consumer never got partition assignments"
            corr = f"test-{int(time.time())}"
            event = {"dpd_release_spike": 2.5, "window_min": 40,
                     "source": "UC-II", "correlation_id": corr}
            producer = kafka_io.get_producer()
            kafka_io.produce(producer, uc2_bridge.TOPIC_DPD_RELEASE, event,
                             key="dpd", flush=True,
                             event_type="jnpa.crosstwin.dpd_release",
                             source_system="TEST", raw_ref="pytest://uc2_bridge")
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                try:
                    evt = await uc2_bridge.next_event(timeout=deadline - time.monotonic())
                except asyncio.TimeoutError:
                    break
                if evt.get("correlation_id") == corr:
                    return evt
            raise AssertionError("published event never came back through the broker")
        finally:
            await uc2_bridge.stop_listener()

    evt = asyncio.run(_roundtrip())
    assert evt["dpd_release_spike"] == 2.5


# --------------------------------------------------------------------------- e2e
RUNNER = "http://localhost:8400"


def _runner_up() -> Optional[str]:
    try:
        with socket.create_connection(("localhost", 8400), timeout=2.0):
            return RUNNER
    except OSError:
        return None


pytestmark_e2e = pytest.mark.skipif(
    _runner_up() is None,
    reason="scenarios-runner not reachable on localhost:8400; run `make up` first.",
)


def _run_and_assert(name: str, params: dict, *, min_steps: int = 5) -> str:
    """Run a scenario, poll its timeline until >= min_steps, return handle_id."""
    import httpx

    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{RUNNER}/scenarios/{name}/run", json=params)
        assert r.status_code == 200, r.text
        handle_id = r.json()["handle_id"]
        assert handle_id

        # Poll the timeline until the 5 dashboard steps have fired.
        steps = []
        for _ in range(30):
            tl = c.get(f"{RUNNER}/scenarios/{handle_id}/timeline")
            if tl.status_code == 200:
                steps = tl.json().get("steps", [])
                if len(steps) >= min_steps:
                    break
            time.sleep(1.0)
        assert len(steps) >= min_steps, f"{name}: only {len(steps)} steps fired: {steps}"

        # Reset cleanly.
        rr = c.post(f"{RUNNER}/scenarios/{name}/reset", json={"handle_id": handle_id})
        assert rr.status_code == 200, rr.text
        assert rr.json().get("ok") is True
    return handle_id


@pytestmark_e2e
def test_tfc1_end_to_end():
    _run_and_assert("tfc1", {"gate_id": "G-NSICT", "duration_minutes": 120})


@pytestmark_e2e
def test_tfc2_end_to_end():
    _run_and_assert("tfc2", {"camera_id": "C-KARAL-EXIT"})


@pytestmark_e2e
def test_tfc3_end_to_end():
    _run_and_assert("tfc3", {"dpd_release_spike": 2.5})
