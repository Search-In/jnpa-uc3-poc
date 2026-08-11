"""Plausibility guards — the defence against confidently wrong figures.

Every figure these guards catch was produced by *correct arithmetic over real
rows*. That is what makes them dangerous: the engine's "never fabricate" rule
does not fire, because nothing was fabricated. There simply were not enough rows,
or the rows were placeholders.

The three cases below are all real, observed against JNPA's RDS on 11 Aug 2026:

* ``teu_per_trip = 50`` — 250 road TEU over **5** ``core.eir`` rows
* ``gate_sustained_rate = 10/h`` — a 16-row ``core.tas_appointment`` stub, badged
  MEASURED, against an **observed peak of 284/h**
* ``moves_per_hour = 10.0`` for min, max *and* mean — a constant, not a fleet

A guard never substitutes a better number; that would be the fabrication it
exists to prevent. It rejects, explains, and lets the scenario fall through or
refuse.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(REPO_ROOT / "shared"), str(REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("POSTGRES_DSN", "postgresql+asyncpg://x:x@127.0.0.1:1/none")

from services.cargo.simulation.plausibility import (  # noqa: E402
    BERTH_MOVES_PER_HOUR, GATE_TRUCKS_PER_HOUR, MIN_SAMPLE_FOR_RATE,
    TEU_PER_TRIP, declared_beats_observed, from_sample, in_band, is_degenerate)


# ------------------------------------------------------------------- in_band
def test_a_plausible_figure_passes():
    assert in_band("teu_per_trip", 1.6, TEU_PER_TRIP)


def test_the_real_50_teu_per_trip_case_is_caught():
    """The figure that understated JNPA's extra truck count by ~25x."""
    verdict = in_band("teu_per_trip", 50.0, TEU_PER_TRIP)
    assert not verdict
    assert "exceeds the physically plausible maximum" in verdict.reason


def test_a_missing_value_is_not_silently_plausible():
    assert not in_band("teu_per_trip", None, TEU_PER_TRIP)


def test_band_edges_are_inclusive():
    assert in_band("x", TEU_PER_TRIP[0], TEU_PER_TRIP)
    assert in_band("x", TEU_PER_TRIP[1], TEU_PER_TRIP)


def test_verdict_is_falsy_so_it_reads_as_a_condition():
    """`if verdict:` must work — the call sites depend on it."""
    assert bool(in_band("x", 1.6, TEU_PER_TRIP)) is True
    assert bool(in_band("x", 50.0, TEU_PER_TRIP)) is False


# ---------------------------------------------------------------- from_sample
def test_a_rate_from_five_observations_is_rejected_regardless_of_value():
    """Sample size is checked FIRST: 1.5 TEU/trip is a fine number and still
    indefensible from five gate records."""
    verdict = from_sample("teu_per_trip", 1.5, 5, TEU_PER_TRIP)
    assert not verdict
    assert verdict.thin_sample is True
    assert "only 5 observation" in verdict.reason


def test_a_rate_from_enough_observations_is_judged_on_value():
    assert from_sample("teu_per_trip", 1.5, MIN_SAMPLE_FOR_RATE, TEU_PER_TRIP)
    bad = from_sample("teu_per_trip", 50.0, MIN_SAMPLE_FOR_RATE, TEU_PER_TRIP)
    assert not bad and bad.thin_sample is False


# -------------------------------------------------- declared_beats_observed
def test_the_real_tas_stub_case_is_caught():
    """10/h declared against 284/h observed: the declaration is a stub."""
    verdict = declared_beats_observed(10.0, 284.0)
    assert not verdict
    assert "floor on capacity" in verdict.reason


def test_a_declaration_above_the_observed_peak_is_accepted():
    """Policy capacity above what was observed is normal — the gate simply was
    not saturated in this window."""
    assert declared_beats_observed(300.0, 284.0)


def test_no_observation_means_the_declaration_stands():
    """With nothing observed there is no floor to contradict it."""
    assert declared_beats_observed(10.0, 0.0)
    assert declared_beats_observed(10.0, None)


# --------------------------------------------------------------- degenerate
def test_the_real_constant_productivity_case_is_caught():
    """min = max = mean = 10.0 across every call is a placeholder column."""
    assert is_degenerate([10.0, 10.0, 10.0]) is True


def test_a_real_fleet_has_spread():
    assert is_degenerate([23.1, 58.2, 164.9]) is False


def test_two_identical_values_are_not_enough_to_conclude():
    """Two calls can legitimately match; three is the signal."""
    assert is_degenerate([10.0, 10.0]) is False


# ------------------------------------------- the bands describe real operations
def test_bands_admit_the_measured_jnpa_corpus_range():
    """The guards must not reject the real world: the JNPA corpus spans 23.1 to
    164.9 berth-moves/hour, and gates peak near 284 trucks/hour."""
    assert in_band("moves", 23.1, BERTH_MOVES_PER_HOUR)
    assert in_band("moves", 164.9, BERTH_MOVES_PER_HOUR)
    assert in_band("gate", 284.0, GATE_TRUCKS_PER_HOUR)
    assert in_band("gate", 269.0, GATE_TRUCKS_PER_HOUR)


def test_bands_reject_the_observed_absurdities():
    assert not in_band("gate", 4010.0, GATE_TRUCKS_PER_HOUR)   # completions p90
    assert not in_band("moves", 0.0, BERTH_MOVES_PER_HOUR)


def test_the_two_guards_catch_different_faults():
    """10/h is physically possible — a one-lane gate could run at it — so the
    band alone cannot reject the TAS stub. It takes the observation to show the
    declaration is wrong. The guards are complementary, not redundant, and this
    is why both are wired into derive_sustained_rate."""
    assert in_band("gate", 10.0, GATE_TRUCKS_PER_HOUR)          # band: fine
    assert not declared_beats_observed(10.0, 284.0)             # vs observed: not fine
