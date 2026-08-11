"""T-01 corridor congestion heatmap (UC3-020).

13 NH-348 segments coloured by a congestion index (flow/capacity adjusted by
speed), over a -6 h … +2 h slider whose DATA_MODE banner flips exactly at now.
A forecast jam probability >= 0.7 recommends a pre-emptive reroute.
"""

from .repository import CorridorHeatmapRepository, to_replay_instant
from .service import (
    REROUTE_PROBABILITY_THRESHOLD,
    CorridorHeatmapService,
    congestion_band,
    congestion_index,
    data_mode_at,
    forecast_confidence,
    jam_probability,
)

__all__ = ["CorridorHeatmapRepository", "CorridorHeatmapService", "to_replay_instant",
           "congestion_index", "congestion_band", "jam_probability", "data_mode_at",
           "forecast_confidence", "REROUTE_PROBABILITY_THRESHOLD"]
