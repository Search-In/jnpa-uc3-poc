"""Abandoned-vehicle rule.

Per the bid spec: a stationary track in a NON-parking area for longer than the
dwell threshold (default 120 s) -> ABANDONED.

"Non-parking area" means *outside* every named NO_PARK_ZONES polygon — a vehicle
stopped inside a no-park zone is handled by the stricter illegal-parking rule
(``rules/parking.py``) instead, so the two rules are mutually exclusive on the
same track and we don't double-alert. Stationarity uses the shared
``motion.stationary_dwell`` primitive (small radius + low speed over a trailing
window).
"""
from __future__ import annotations

from typing import Optional

from jnpa_shared.corridor import zone_for_point
from jnpa_shared.schemas import Alert

from ..config import AnomalyConfig
from ..motion import stationary_dwell
from ..types import Track

KIND = "ABANDONED"


def evaluate(track: Track, cfg: AnomalyConfig) -> Optional[Alert]:
    """Return an ABANDONED Alert if the track is long-stationary outside no-park zones."""
    last = track.latest
    if last is None:
        return None

    # Inside a no-park zone -> parking rule's jurisdiction, not abandonment.
    if zone_for_point(last.lat, last.lon) is not None:
        return None

    dwell_s = stationary_dwell(
        track,
        speed_kmh_max=cfg.stationary_speed_kmh,
        radius_m=cfg.stationary_radius_m,
    )
    if dwell_s < cfg.abandoned_dwell_s:
        return None

    return Alert(
        kind=KIND,
        severity="warning",
        plate=track.plate,
        payload={
            "track_id": track.track_id,
            "camera_id": track.camera_id,
            "device_id": track.device_id,
            "dwell_s": round(dwell_s, 1),
            "threshold_s": cfg.abandoned_dwell_s,
            "lat": last.lat,
            "lon": last.lon,
            "ts": last.ts.isoformat(),
        },
    )


__all__ = ["KIND", "evaluate"]
