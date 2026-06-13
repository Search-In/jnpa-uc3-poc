"""Virtual RTSP-like replay of MP4 clips.

Each clip under ``clips_dir`` is opened with OpenCV and replayed in a loop at a
target sampling FPS. The replayer yields ``(camera_id, frame, ts)`` tuples,
round-robining across all clips so every camera makes progress. Timestamps are
timezone-aware UTC (wall-clock at emission, matching a live RTSP feed).

If there are zero clips the replayer yields nothing — the caller is expected to
emit ``no_feed`` health events on its own cadence.
"""
from __future__ import annotations

import asyncio
import glob
import os
from datetime import datetime, timezone
from typing import AsyncIterator, List, Optional, Tuple

import cv2
import numpy as np

from jnpa_shared.logging import get_logger

from .config import AnprConfig, camera_id_for_clip

log = get_logger("anpr_ingest.replay")

Frame = np.ndarray
FrameTuple = Tuple[str, Frame, datetime]


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def discover_clips(clips_dir: str) -> List[str]:
    """Return readable MP4 paths in clips_dir (sorted, deduped)."""
    paths: List[str] = []
    for pattern in ("*.mp4", "*.MP4", "*.mov", "*.mkv"):
        paths.extend(glob.glob(os.path.join(clips_dir, pattern)))
    # Keep only non-empty files (placeholders may be 0 bytes).
    real = sorted({p for p in paths if os.path.isfile(p) and os.path.getsize(p) > 1024})
    return real


class _ClipFeed:
    """A single looping clip, sampled down to target_fps."""

    def __init__(self, path: str, target_fps: float) -> None:
        self.path = path
        self.stem = os.path.splitext(os.path.basename(path))[0]
        self.camera_id = camera_id_for_clip(self.stem)
        self.target_fps = max(0.1, target_fps)
        self._cap: Optional[cv2.VideoCapture] = None
        self._native_fps = 25.0
        self._stride = 1
        self._frame_no = 0

    def _open(self) -> bool:
        self._cap = cv2.VideoCapture(self.path)
        if not self._cap or not self._cap.isOpened():
            log.warning("clip_open_failed", path=self.path)
            return False
        native = self._cap.get(cv2.CAP_PROP_FPS) or 0.0
        self._native_fps = native if native and native > 0 else 25.0
        self._stride = max(1, int(round(self._native_fps / self.target_fps)))
        log.info(
            "clip_opened",
            path=self.path,
            camera_id=self.camera_id,
            native_fps=round(self._native_fps, 2),
            stride=self._stride,
        )
        return True

    def read_sampled(self) -> Optional[Frame]:
        """Return the next sampled frame, looping at EOF. None if unreadable."""
        if self._cap is None and not self._open():
            return None
        assert self._cap is not None
        # Skip `stride-1` frames, then take one.
        for _ in range(self._stride):
            ok, frame = self._cap.read()
            self._frame_no += 1
            if not ok:
                # Loop the clip.
                self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = self._cap.read()
                if not ok:
                    return None
        return frame

    def release(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class Replayer:
    """Round-robin replayer over all discovered clips."""

    def __init__(self, cfg: AnprConfig) -> None:
        self.cfg = cfg
        self.feeds: List[_ClipFeed] = []

    def refresh_feeds(self) -> int:
        """(Re)discover clips. Returns the number of active feeds."""
        paths = discover_clips(self.cfg.clips_dir)
        existing = {f.path for f in self.feeds}
        if set(paths) != existing:
            for f in self.feeds:
                f.release()
            self.feeds = [_ClipFeed(p, self.cfg.target_fps) for p in paths]
            log.info("feeds_refreshed", count=len(self.feeds),
                     cameras=[f.camera_id for f in self.feeds])
        return len(self.feeds)

    async def frames(self) -> AsyncIterator[FrameTuple]:
        """Async generator of (camera_id, frame, ts), round-robin across feeds.

        Sleeps to roughly honour target_fps. Yields nothing when no clips exist;
        the caller handles the no_feed path.
        """
        self.refresh_feeds()
        period = 1.0 / max(0.1, self.cfg.target_fps)
        while True:
            if not self.feeds:
                # Nothing to replay; let the event loop breathe and recheck.
                await asyncio.sleep(self.cfg.no_feed_interval_s)
                self.refresh_feeds()
                continue
            for feed in list(self.feeds):
                frame = feed.read_sampled()
                if frame is None:
                    continue
                yield (feed.camera_id, frame, _utcnow())
                await asyncio.sleep(period / max(1, len(self.feeds)))

    def close(self) -> None:
        for f in self.feeds:
            f.release()
        self.feeds = []
