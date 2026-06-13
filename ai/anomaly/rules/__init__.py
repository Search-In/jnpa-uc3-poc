"""Rule engine for the behavioural anomaly detector.

Each rule is a small, side-effect-free analyzer: it takes a ``Track`` (and the
shared ``AnomalyConfig``) and returns an ``Alert`` or ``None``. The engine
(``ai.anomaly.engine``) runs every rule over every closed/updated track and the
autoencoder over the same track features, dedupes, and emits the surviving
alerts. Keeping rules pure makes the synthetic test scenarios trivial: build a
``Track``, call the rule, assert on the single alert.

``CAMERA_ALLOWED_BEARING`` is the per-camera "allowed travel direction" used by
the wrong-way rule: each corridor/gate camera looks along the carriageway, so a
vehicle's heading should sit within a tolerance band of the camera's nominal
downstream bearing. Bearings are compass degrees (0=N, clockwise) derived from
the NH-348 corridor geometry (port end -> Karal Phata is broadly south-east).
"""
from __future__ import annotations

from typing import Dict

# Nominal "with-traffic" bearing per camera (degrees, 0=N clockwise). The
# corridor runs port(NW) -> Karal Phata(SE), so the legal downstream heading is
# ~135° (SE) for corridor cameras; gate entry/exit lanes face the gate apron.
# A track heading more than cfg.wrongway_divergence_deg from this is wrong-way.
CAMERA_ALLOWED_BEARING: Dict[str, float] = {
    # Corridor cameras (downstream = toward Karal Phata, ~SE).
    "CAM-COR-01": 135.0,
    "CAM-COR-02": 135.0,
    "CAM-COR-03": 130.0,
    "CAM-COR-04": 120.0,
    "CAM-COR-05": 110.0,
    "CAM-COR-06": 110.0,
    # Gate lanes: entry traffic flows INTO the port (~NW, ~315°); exit flows out.
    "CAM-NSICT-ENT": 315.0,
    "CAM-NSICT-EXT": 135.0,
    "CAM-JNPCT-ENT": 315.0,
    "CAM-JNPCT-EXT": 135.0,
}

# Fallback when a camera id is unknown: assume the downstream corridor bearing.
DEFAULT_ALLOWED_BEARING = 135.0


def allowed_bearing(camera_id: str | None) -> float:
    """Allowed (with-traffic) compass bearing for a camera, with a fallback."""
    if camera_id is None:
        return DEFAULT_ALLOWED_BEARING
    return CAMERA_ALLOWED_BEARING.get(camera_id, DEFAULT_ALLOWED_BEARING)


__all__ = [
    "CAMERA_ALLOWED_BEARING",
    "DEFAULT_ALLOWED_BEARING",
    "allowed_bearing",
]
