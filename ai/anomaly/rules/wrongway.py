"""Wrong-way driving rule.

Per the bid spec: each camera has an "allowed bearing range"; if a track's
heading diverges from that allowed (with-traffic) bearing by more than the
configured threshold (default 120°) for longer than the hold window (default
2 s), raise a WRONG_WAY alert.

The track's heading at each step is taken from the observation's ``heading``
field when present (telemetry/synthetic), else derived from the bearing between
consecutive (lat, lon) points (ByteTrack-projected tracks). We require the
divergence to be *sustained* — a single frame of a turning vehicle facing
upstream must not trip it — so we find the longest trailing run of consecutive
diverging steps and compare its span to the hold window.
"""
from __future__ import annotations

from typing import Optional

from jnpa_shared.schemas import Alert

from ..config import AnomalyConfig
from ..types import Track, angular_diff_deg, bearing_deg
from . import allowed_bearing

KIND = "WRONG_WAY"


def _step_heading(track: Track, i: int) -> Optional[float]:
    """Heading at point i: use the recorded heading, else bearing from prev pt."""
    p = track.points[i]
    if p.heading:
        return p.heading
    if i > 0:
        prev = track.points[i - 1]
        if (prev.lat, prev.lon) != (p.lat, p.lon):
            return bearing_deg((prev.lat, prev.lon), (p.lat, p.lon))
    return None


def evaluate(track: Track, cfg: AnomalyConfig) -> Optional[Alert]:
    """Return a WRONG_WAY Alert if the track sustains an against-traffic heading."""
    if len(track.points) < 2:
        return None

    want = allowed_bearing(track.camera_id)

    # Walk backward accumulating the trailing run of diverging steps; the run's
    # time span is how long the vehicle has been heading against traffic.
    run_start_idx: Optional[int] = None
    max_divergence = 0.0
    for i in range(len(track.points) - 1, -1, -1):
        h = _step_heading(track, i)
        if h is None:
            break
        div = angular_diff_deg(h, want)
        if div <= cfg.wrongway_divergence_deg:
            break
        run_start_idx = i
        max_divergence = max(max_divergence, div)

    if run_start_idx is None:
        return None

    span_s = (track.points[-1].ts - track.points[run_start_idx].ts).total_seconds()
    if span_s < cfg.wrongway_hold_s:
        return None

    last = track.points[-1]
    return Alert(
        kind=KIND,
        severity="critical",
        plate=track.plate,
        payload={
            "track_id": track.track_id,
            "camera_id": track.camera_id,
            "device_id": track.device_id,
            "allowed_bearing_deg": round(want, 1),
            "observed_bearing_deg": round(_step_heading(track, len(track.points) - 1) or 0.0, 1),
            "max_divergence_deg": round(max_divergence, 1),
            "sustained_s": round(span_s, 2),
            "lat": last.lat,
            "lon": last.lon,
            "ts": last.ts.isoformat(),
        },
    )


__all__ = ["KIND", "evaluate"]
