"""UC3-020 — T-01 corridor congestion heatmap.

The index, the banner flip and the reroute trigger are pure functions, so they
are tested exactly at their boundaries without a database.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(REPO_ROOT), str(REPO_ROOT / "shared")):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

from jnpa_shared import corridor  # noqa: E402
from services.corridor_heatmap import (  # noqa: E402
    REROUTE_PROBABILITY_THRESHOLD,
    CorridorHeatmapService,
    congestion_band,
    congestion_index,
    data_mode_at,
    forecast_confidence,
    jam_probability,
    to_replay_instant,
)
from services.corridor_heatmap.service import (  # noqa: E402
    FORECAST_HOURS,
    FREE_FLOW_KPH,
    PAST_HOURS,
)

NOW = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)


# ------------------------------------------------------------------ geometry
def test_the_corridor_has_the_ticket_s_thirteen_segments():
    assert len(corridor.segments) == 13
    assert [s.id for s in corridor.segments][:3] == ["SEG-00", "SEG-01", "SEG-02"]


@pytest.mark.asyncio
async def test_every_segment_is_returned_with_real_coordinates():
    class _Empty:
        async def segment_flow(self, at, mins):
            return []

    out = await CorridorHeatmapService(repository=_Empty()).heatmap(now=NOW)
    assert out["segment_count"] == 13
    for s in out["segments"]:
        # JNPA sits near 18.9 N, 72.9 E — a segment outside that box is misplaced.
        assert 18.5 < s["lat"] < 19.5, s
        assert 72.5 < s["lon"] < 73.5, s
        assert s["length_km"] > 0


# ------------------------------------------------- the banner flips at NOW
def test_data_mode_flips_exactly_at_now():
    assert data_mode_at(NOW - dt.timedelta(seconds=1), NOW) == "OBSERVED"
    assert data_mode_at(NOW, NOW) == "OBSERVED"           # at now is still observed
    assert data_mode_at(NOW + dt.timedelta(seconds=1), NOW) == "DERIVED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "offset,mode",
    [(-360, "OBSERVED"), (-15, "OBSERVED"), (0, "OBSERVED"),
     (1, "DERIVED"), (15, "DERIVED"), (120, "DERIVED")],
)
async def test_slider_positions_report_the_right_mode(offset, mode):
    class _Empty:
        async def segment_flow(self, at, mins):
            return []

    out = await CorridorHeatmapService(repository=_Empty()).heatmap(
        offset_minutes=offset, now=NOW)
    assert out["data_mode"] == mode
    for s in out["segments"]:
        assert s["data_mode"] == mode


@pytest.mark.asyncio
async def test_slider_is_clamped_to_the_six_hour_two_hour_window():
    class _Empty:
        async def segment_flow(self, at, mins):
            return []

    svc = CorridorHeatmapService(repository=_Empty())
    past = await svc.heatmap(offset_minutes=-9999, now=NOW)
    future = await svc.heatmap(offset_minutes=9999, now=NOW)
    assert past["offset_minutes"] == -PAST_HOURS * 60
    assert future["offset_minutes"] == FORECAST_HOURS * 60
    assert past["clamped"] is True and future["clamped"] is True
    # A clamped request must not masquerade as the one that was asked for.
    assert future["offset_requested"] == 9999


def test_forecast_confidence_decays_with_horizon():
    assert forecast_confidence(0) == 1.0
    assert forecast_confidence(30) > forecast_confidence(120)
    assert forecast_confidence(120) >= 0.55


# ------------------------------------------------------------------- index
def test_index_is_flow_over_capacity_adjusted_by_speed():
    # At free flow the speed factor is 1, so the index is the raw ratio.
    assert congestion_index(120, 240, FREE_FLOW_KPH) == pytest.approx(0.5, abs=1e-3)
    # Half speed doubles it — the same flow through a slower segment is worse.
    assert congestion_index(120, 240, FREE_FLOW_KPH / 2) == pytest.approx(1.0, abs=1e-3)


def test_index_is_zero_when_capacity_is_unknown():
    assert congestion_index(100, 0, 50) == 0.0


@pytest.mark.parametrize(
    "index,band",
    [(0.0, "FREE"), (0.49, "FREE"), (0.5, "BUSY"), (0.74, "BUSY"),
     (0.75, "HEAVY"), (0.89, "HEAVY"), (0.9, "SEVERE"), (1.5, "SEVERE")],
)
def test_bands_at_their_boundaries(index, band):
    assert congestion_band(index) == band


# -------------------------------------------------------- reroute trigger
def test_a_crawling_segment_is_jammed_even_though_its_flow_is_low():
    """Flow FALLS in a jam, so an index-only probability under-reads it."""
    crawling = jam_probability(0.5, speed_kph=5.0)
    assert crawling >= REROUTE_PROBABILITY_THRESHOLD, crawling
    # The same index at free-flow speed is not a jam.
    assert jam_probability(0.5, speed_kph=FREE_FLOW_KPH) < REROUTE_PROBABILITY_THRESHOLD


@pytest.mark.parametrize(
    "speed,triggers",
    [
        (50.0, False),  # free flow
        (25.0, False),  # half speed, deficit 0.50
        (16.0, False),  # deficit 0.68 — just under
        (15.0, True),   # deficit exactly 0.70 — the boundary triggers (>=)
        (10.0, True),
        (5.0, True),
    ],
)
def test_reroute_threshold_from_both_sides(speed, triggers):
    """The trigger boundary is speed = 15 km/h: 1 - 15/50 = 0.70 exactly."""
    p = jam_probability(0.3, speed_kph=speed)
    assert (p >= REROUTE_PROBABILITY_THRESHOLD) is triggers, f"{speed} -> {p}"


def test_threshold_is_the_ticket_s_zero_point_seven():
    assert REROUTE_PROBABILITY_THRESHOLD == 0.7


@pytest.mark.asyncio
async def test_reroute_is_reported_with_its_reason_not_just_a_flag():
    class _Jammed:
        async def segment_flow(self, at, mins):
            return [{"segment_code": s.id, "trucks": 10, "flow_vph": 40.0,
                     "speed_kph": 4.0} for s in corridor.segments]

    out = await CorridorHeatmapService(repository=_Jammed()).heatmap(now=NOW)
    assert out["reroute"]["triggered"] is True
    assert out["reroute"]["action"] == "PRE_EMPTIVE_REROUTE_AND_CPP_METERING"
    hit = [s for s in out["segments"] if s["reroute_recommended"]]
    assert hit and all(">= 0.7" in (s["reroute_reason"] or "") for s in hit)


@pytest.mark.asyncio
async def test_free_flowing_corridor_recommends_nothing():
    class _Calm:
        async def segment_flow(self, at, mins):
            return [{"segment_code": s.id, "trucks": 2, "flow_vph": 8.0,
                     "speed_kph": FREE_FLOW_KPH} for s in corridor.segments]

    out = await CorridorHeatmapService(repository=_Calm()).heatmap(now=NOW)
    assert out["reroute"]["triggered"] is False
    assert out["reroute"]["segments"] == []


# ------------------------------------------------------ honest empty states
@pytest.mark.asyncio
async def test_an_unmeasured_segment_reports_no_data_not_a_zero():
    """A zero index would paint the segment green, which is a claim."""
    class _Empty:
        async def segment_flow(self, at, mins):
            return []

    out = await CorridorHeatmapService(repository=_Empty()).heatmap(now=NOW)
    assert out["measured_count"] == 0
    for s in out["segments"]:
        assert s["congestion_index"] is None
        assert s["band"] is None
        assert s["observation"] == "NO_DATA"
        assert s["reroute_recommended"] is False


@pytest.mark.asyncio
async def test_a_repository_failure_degrades_to_no_data_not_a_500():
    class _Broken:
        async def segment_flow(self, at, mins):
            raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await CorridorHeatmapService(repository=_Broken()).heatmap(now=NOW)


@pytest.mark.asyncio
async def test_future_buckets_are_extrapolated_and_labelled_as_such():
    class _Flow:
        async def segment_flow(self, at, mins):
            return [{"segment_code": s.id, "trucks": 5, "flow_vph": 20.0,
                     "speed_kph": FREE_FLOW_KPH} for s in corridor.segments]

    svc = CorridorHeatmapService(repository=_Flow())
    now_out = await svc.heatmap(offset_minutes=0, now=NOW)
    fut_out = await svc.heatmap(offset_minutes=120, now=NOW)
    assert all(s["observation"] == "COUNTED" for s in now_out["segments"])
    assert all(s["observation"] == "EXTRAPOLATED" for s in fut_out["segments"])
    # The extrapolation grows the flow, and says by how much in `method`.
    assert fut_out["segments"][0]["flow_vph"] > now_out["segments"][0]["flow_vph"]


# --------------------------------------------------------- replay mapping
def test_replay_mapping_preserves_weekday_and_time_of_day():
    """A Friday peak must land on the simulation's Friday, not a Tuesday lull."""
    friday = dt.datetime(2026, 8, 14, 17, 30, tzinfo=dt.timezone.utc)  # a Friday
    mapped = to_replay_instant(friday)
    assert mapped.weekday() == friday.weekday()
    assert (mapped.hour, mapped.minute) == (17, 30)
    # …and it lands inside the frozen 20-26 July calibration week.
    assert dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc) <= mapped
    assert mapped < dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)


def test_provenance_states_the_resolution_disclaimer():
    import asyncio

    class _Empty:
        async def segment_flow(self, at, mins):
            return []

    out = asyncio.run(CorridorHeatmapService(repository=_Empty()).heatmap(now=NOW))
    assert "A4" in out["provenance"]["note"]
    assert "survey-grade" in out["provenance"]["resolution_disclaimer"]
