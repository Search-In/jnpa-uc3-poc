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

    assert set(scenario_names()) == {"tfc1", "tfc2", "tfc3", "monsoon_friday"}
    # The master scenario exposes the run/reset contract like the others.
    mf = get_scenario("monsoon_friday")
    assert mf is not None and callable(mf.run) and callable(mf.reset)


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
