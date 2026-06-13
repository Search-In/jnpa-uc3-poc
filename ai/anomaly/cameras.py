"""Static camera metadata for image→ground projection.

The corridor/gate cameras and their ground coordinates mirror the
``jnpa.cameras`` seed rows in ``infra/postgres/init.sql``. For the PoC we use a
deliberately simple pinhole-free projection: a detection's image-space centre is
mapped to a small (lat, lon) offset around the camera's mounted location, scaled
by how far the bbox sits from the frame's horizontal/vertical centre and the
camera's nominal field-of-view footprint. This is enough to give the rule engine
a plausible geo-track for a fixed corridor camera (which only sees a short span
of carriageway) without per-camera homography calibration data we don't have in
a PoC. Telemetry-sourced tracks bypass this entirely (they carry real GPS).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class CameraGeo:
    """A camera's mounted ground location + the footprint it observes."""

    id: str
    lat: float
    lon: float
    # Approx ground footprint half-extents (metres) the frame spans, used to map
    # normalised image offsets to ground offsets.
    span_m: float = 60.0
    # Bearing (deg) the camera's "up in image" direction points on the ground.
    bearing_deg: float = 135.0


# Mirrors the jnpa.cameras seed (corridor + the two NSICT gate lanes used here).
CAMERAS: Dict[str, CameraGeo] = {
    "CAM-COR-01": CameraGeo("CAM-COR-01", 18.9100, 72.9700, 80.0, 135.0),
    "CAM-COR-02": CameraGeo("CAM-COR-02", 18.8850, 72.9900, 80.0, 135.0),
    "CAM-COR-03": CameraGeo("CAM-COR-03", 18.8600, 73.0100, 80.0, 130.0),
    "CAM-COR-04": CameraGeo("CAM-COR-04", 18.8400, 73.0300, 80.0, 120.0),
    "CAM-COR-05": CameraGeo("CAM-COR-05", 18.8150, 73.0550, 80.0, 110.0),
    "CAM-COR-06": CameraGeo("CAM-COR-06", 18.7800, 73.0800, 80.0, 110.0),
    "CAM-NSICT-ENT": CameraGeo("CAM-NSICT-ENT", 18.9491, 72.9490, 40.0, 315.0),
    "CAM-NSICT-EXT": CameraGeo("CAM-NSICT-EXT", 18.9487, 72.9494, 40.0, 135.0),
}


def get(camera_id: str) -> Optional[CameraGeo]:
    return CAMERAS.get(camera_id)


def project(
    camera_id: str, cx: float, cy: float, frame_w: int, frame_h: int
) -> Optional[Tuple[float, float]]:
    """Project an image-space bbox centre (cx, cy) to a ground (lat, lon).

    Maps the normalised offset from frame centre to a ground offset along the
    camera's viewing bearing (vertical image axis = along-carriageway) and the
    perpendicular (horizontal image axis = across-carriageway), then converts the
    metre offset to a (lat, lon) delta around the camera's mounted location.
    Returns ``None`` for an uncalibrated camera.
    """
    cam = CAMERAS.get(camera_id)
    if cam is None or frame_w <= 0 or frame_h <= 0:
        return None

    # Normalised offsets in [-1, 1]; image y grows downward, so invert for
    # "further from camera = up in frame = +along bearing".
    nx = (cx - frame_w / 2.0) / (frame_w / 2.0)
    ny = -(cy - frame_h / 2.0) / (frame_h / 2.0)

    along_m = ny * cam.span_m       # along the viewing bearing
    across_m = nx * cam.span_m      # perpendicular (to the right of bearing)

    brg = math.radians(cam.bearing_deg)
    # North/East components: along bearing + across (bearing+90°).
    north_m = along_m * math.cos(brg) + across_m * math.cos(brg + math.pi / 2)
    east_m = along_m * math.sin(brg) + across_m * math.sin(brg + math.pi / 2)

    dlat = north_m / 111_320.0
    dlon = east_m / (111_320.0 * math.cos(math.radians(cam.lat)))
    return (cam.lat + dlat, cam.lon + dlon)


__all__ = ["CameraGeo", "CAMERAS", "get", "project"]
