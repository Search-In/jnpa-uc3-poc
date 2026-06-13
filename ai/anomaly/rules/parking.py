"""Illegal-parking rule with duration-based escalation.

Per the bid spec: a stationary track inside any of the 6 named no-parking
polygons (``jnpa_shared.corridor.NO_PARK_ZONES``) for longer than the dwell
threshold (default 300 s) -> ILLEGAL_PARKING, escalating by dwell duration:

    >= 5  min  -> severity WARNING
    >= 15 min  -> severity CRITICAL
    >= 30 min  -> severity REPORT_TO_POLICE

The escalation level is recorded both as the alert ``severity`` and an explicit
``escalation`` field in the payload so downstream consumers can route the 30-min
case to the police-reporting workflow.
"""
from __future__ import annotations

from typing import Optional, Tuple

from jnpa_shared.corridor import zone_for_point
from jnpa_shared.schemas import Alert

from ..config import AnomalyConfig
from ..motion import stationary_dwell
from ..types import Track

KIND = "ILLEGAL_PARKING"

# Escalation labels (severity carried on the Alert + an explicit payload field).
ESCALATION_WARNING = "WARNING"
ESCALATION_CRITICAL = "CRITICAL"
ESCALATION_POLICE = "REPORT_TO_POLICE"


def _escalation(dwell_s: float, cfg: AnomalyConfig) -> Tuple[str, str]:
    """Map a dwell duration to (escalation_label, alert_severity)."""
    if dwell_s >= cfg.parking_police_s:
        return ESCALATION_POLICE, "critical"
    if dwell_s >= cfg.parking_critical_s:
        return ESCALATION_CRITICAL, "critical"
    return ESCALATION_WARNING, "warning"


def evaluate(track: Track, cfg: AnomalyConfig) -> Optional[Alert]:
    """Return an ILLEGAL_PARKING Alert if the track is long-stationary in a no-park zone."""
    last = track.latest
    if last is None:
        return None

    zone = zone_for_point(last.lat, last.lon)
    if zone is None:
        return None

    dwell_s = stationary_dwell(
        track,
        speed_kmh_max=cfg.stationary_speed_kmh,
        radius_m=cfg.stationary_radius_m,
    )
    if dwell_s < cfg.parking_dwell_s:
        return None

    escalation, severity = _escalation(dwell_s, cfg)
    return Alert(
        kind=KIND,
        severity=severity,
        plate=track.plate,
        payload={
            "track_id": track.track_id,
            "camera_id": track.camera_id,
            "device_id": track.device_id,
            "zone_id": zone.id,
            "zone_name": zone.name,
            "dwell_s": round(dwell_s, 1),
            "dwell_min": round(dwell_s / 60.0, 1),
            "escalation": escalation,
            "lat": last.lat,
            "lon": last.lon,
            "ts": last.ts.isoformat(),
        },
    )


__all__ = [
    "KIND",
    "ESCALATION_WARNING",
    "ESCALATION_CRITICAL",
    "ESCALATION_POLICE",
    "evaluate",
]
