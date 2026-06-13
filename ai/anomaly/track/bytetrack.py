"""ByteTrack vehicle tracking via the supervision wrapper + a YOLOv8 detector.

Per the bid spec: ByteTrack via the official ``supervision==0.22.*`` wrapper
(``sv.ByteTrack``) fed by a YOLOv8 vehicle detector. Frames arrive on the shared
Redis frame bus (``frames.{camera_id}``) written by ingest/anpr at ~5 fps.

For each camera we run an independent ByteTrack instance: decode the jpeg ->
YOLOv8 detect (filtered to vehicle COCO classes) -> ``tracker.update_with_detections``
-> per tracker-id, append a ``TrackPoint`` (bbox-centre projected to ground via
``cameras.project`` + an estimated speed/heading from successive positions). The
tracker yields *closed* tracks (ids that have not been seen for the lost-track
buffer) and *active* snapshots so the engine can run the rules continuously.

Graceful degradation: ``supervision`` / ``ultralytics`` / ``torch`` are heavy and
GPU-oriented. If any is missing the tracker reports ``available == False`` and
the service runs rules + AE over telemetry-/synthetic-sourced tracks instead
(the live frame-bus path is simply inactive). This mirrors the degrade pattern
the rest of the repo uses (ai/anpr, ai/congestion).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import numpy as np

from jnpa_shared.corridor import haversine_km
from jnpa_shared.logging import get_logger

from ..config import AnomalyConfig
from ..types import Track, TrackPoint, bearing_deg
from .. import cameras

log = get_logger("anomaly.bytetrack")

# COCO vehicle class ids: car, motorcycle, bus, truck.
VEHICLE_CLASS_IDS = (2, 3, 5, 7)
_COCO_TO_CLASS = {2: "CAR", 3: "2W", 5: "BUS", 7: "HGV"}


def deps_available() -> bool:
    """True if supervision + ultralytics + torch are importable."""
    try:
        import supervision  # noqa: F401
        import ultralytics  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@dataclass
class _CameraTracker:
    """Per-camera ByteTrack state + the in-flight tracks it is building."""

    camera_id: str
    tracker: object                                   # sv.ByteTrack
    tracks: Dict[int, Track] = field(default_factory=dict)
    last_seen: Dict[int, datetime] = field(default_factory=dict)


class VehicleTracker:
    """Multi-camera ByteTrack tracker over decoded frames.

    Usage:
        vt = VehicleTracker(cfg)
        if vt.available:
            for closed in vt.update(camera_id, frame_bgr, ts):
                ... run rules over `closed` ...
    """

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        self.available = deps_available()
        self._model = None
        self._trackers: Dict[str, _CameraTracker] = {}
        self._next_global = 0

    # -- lazy model / tracker construction ----------------------------------
    def _ensure_model(self):
        if self._model is None:
            from ultralytics import YOLO

            self._model = YOLO(self.cfg.yolo_weights)
            log.info("yolo_loaded", weights=self.cfg.yolo_weights)
        return self._model

    def _tracker_for(self, camera_id: str) -> _CameraTracker:
        ct = self._trackers.get(camera_id)
        if ct is None:
            import supervision as sv

            bt = sv.ByteTrack(
                track_activation_threshold=self.cfg.track_activation_threshold,
                lost_track_buffer=self.cfg.lost_track_buffer,
                minimum_matching_threshold=self.cfg.minimum_matching_threshold,
                frame_rate=self.cfg.frame_rate,
            )
            ct = _CameraTracker(camera_id=camera_id, tracker=bt)
            self._trackers[camera_id] = ct
        return ct

    # -- per-frame update ---------------------------------------------------
    def update(self, camera_id: str, frame_bgr: np.ndarray, ts: datetime) -> List[Track]:
        """Run detection + tracking on one frame; return tracks that just closed.

        A track "closes" when its tracker-id stops appearing in updates for the
        lost-track buffer — at which point it is final and the engine scores it.
        """
        if not self.available:
            return []
        import supervision as sv

        model = self._ensure_model()
        ct = self._tracker_for(camera_id)
        h, w = frame_bgr.shape[:2]

        result = model(frame_bgr, verbose=False, conf=self.cfg.detect_conf)[0]
        det = sv.Detections.from_ultralytics(result)
        # Keep only vehicle classes.
        if det.class_id is not None and len(det) > 0:
            mask = np.isin(det.class_id, VEHICLE_CLASS_IDS)
            det = det[mask]

        tracked = ct.tracker.update_with_detections(det)

        seen_ids: set[int] = set()
        for xyxy, _mask, _conf, class_id, tracker_id, _data in _iter_detections(tracked):
            if tracker_id is None:
                continue
            tid = int(tracker_id)
            seen_ids.add(tid)
            cx = float((xyxy[0] + xyxy[2]) / 2.0)
            cy = float((xyxy[1] + xyxy[3]) / 2.0)
            geo = cameras.project(camera_id, cx, cy, w, h)
            if geo is None:
                continue
            lat, lon = geo
            track = ct.tracks.get(tid)
            if track is None:
                track = Track(
                    track_id=f"{camera_id}:{tid}",
                    camera_id=camera_id,
                    vehicle_class=_COCO_TO_CLASS.get(int(class_id) if class_id is not None else -1, "UNKNOWN"),
                )
                ct.tracks[tid] = track
            speed, heading = _estimate_motion(track, lat, lon, ts)
            track.add(TrackPoint(ts=ts, lat=lat, lon=lon, speed_kmh=speed,
                                 heading=heading, cx=cx, cy=cy))
            ct.last_seen[tid] = ts

        return self._reap(ct, ts, seen_ids)

    def _reap(self, ct: _CameraTracker, now: datetime, seen_ids: set) -> List[Track]:
        """Close out tracks unseen for longer than the lost-track buffer."""
        buffer_s = self.cfg.lost_track_buffer / max(1, self.cfg.frame_rate)
        closed: List[Track] = []
        for tid in list(ct.tracks.keys()):
            if tid in seen_ids:
                continue
            last = ct.last_seen.get(tid)
            if last is None or (now - last).total_seconds() >= buffer_s:
                closed.append(ct.tracks.pop(tid))
                ct.last_seen.pop(tid, None)
        return closed

    def active_tracks(self) -> List[Track]:
        """Snapshot of all currently-open tracks across cameras."""
        out: List[Track] = []
        for ct in self._trackers.values():
            out.extend(ct.tracks.values())
        return out


def _estimate_motion(track: Track, lat: float, lon: float, ts: datetime):
    """Estimate (speed_kmh, heading_deg) from the previous point to this one."""
    prev = track.latest
    if prev is None:
        return 0.0, 0.0
    dt = (ts - prev.ts).total_seconds()
    if dt <= 0:
        return prev.speed_kmh, prev.heading
    dist_km = haversine_km((prev.lat, prev.lon), (lat, lon))
    speed = dist_km / (dt / 3600.0)
    heading = bearing_deg((prev.lat, prev.lon), (lat, lon)) if dist_km > 1e-6 else prev.heading
    return speed, heading


def _iter_detections(det):
    """Iterate a supervision Detections object (compat shim across versions)."""
    # supervision Detections is iterable yielding
    # (xyxy, mask, confidence, class_id, tracker_id, data) per 0.22.x.
    for row in det:
        yield row


__all__ = ["VehicleTracker", "deps_available", "VEHICLE_CLASS_IDS"]
