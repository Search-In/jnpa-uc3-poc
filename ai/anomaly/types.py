"""Common track data types shared by the tracker, rules, and autoencoder.

A ``Track`` is the unit every detector path produces and every rule consumes:

  * ByteTrack (``track/bytetrack.py``) builds tracks from per-camera frames —
    each ``TrackPoint`` carries the bbox-centre in *image* coordinates plus a
    geo-projection to (lat, lon) for the corridor rules.
  * The trucking-app telemetry path and the synthetic test fixtures build tracks
    directly from (lat, lon, speed, heading) GPS pings — no image needed.

Keeping one type for both means the rule engine and the AE don't care where the
track came from, and the synthetic test scenarios exercise exactly the same code
path the live tracker feeds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple


@dataclass
class TrackPoint:
    """One observation of a track at a moment in time."""

    ts: datetime
    lat: float
    lon: float
    speed_kmh: float = 0.0
    heading: float = 0.0           # degrees, 0=N, clockwise (compass bearing)
    # Optional image-space centre (px) when the point came from a video frame.
    cx: Optional[float] = None
    cy: Optional[float] = None


@dataclass
class Track:
    """A tracked vehicle: an ordered series of observations."""

    track_id: str
    camera_id: Optional[str] = None
    plate: Optional[str] = None
    device_id: Optional[str] = None        # trucking-app device, if GPS-sourced
    vehicle_class: str = "UNKNOWN"
    points: List[TrackPoint] = field(default_factory=list)

    def add(self, point: TrackPoint) -> None:
        self.points.append(point)
        # Cap track history to 1800 points (~5 mins at 6 FPS)
        # to prevent OOM on stationary/long-lived tracks.
        if len(self.points) > 1800:
            self.points = self.points[-1800:]

    @property
    def first_ts(self) -> Optional[datetime]:
        return self.points[0].ts if self.points else None

    @property
    def last_ts(self) -> Optional[datetime]:
        return self.points[-1].ts if self.points else None

    @property
    def duration_s(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return (self.points[-1].ts - self.points[0].ts).total_seconds()

    @property
    def latest(self) -> Optional[TrackPoint]:
        return self.points[-1] if self.points else None

    @property
    def path(self) -> List[Tuple[float, float]]:
        """The (lat, lon) polyline of the track."""
        return [(p.lat, p.lon) for p in self.points]

    def speed_series(self) -> List[float]:
        return [p.speed_kmh for p in self.points]

    def heading_series(self) -> List[float]:
        return [p.heading for p in self.points]


def bearing_deg(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Initial compass bearing (degrees, 0=N, clockwise) from point a to b."""
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def angular_diff_deg(a: float, b: float) -> float:
    """Smallest absolute difference between two bearings (0..180 degrees)."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


__all__ = [
    "TrackPoint",
    "Track",
    "bearing_deg",
    "angular_diff_deg",
]
