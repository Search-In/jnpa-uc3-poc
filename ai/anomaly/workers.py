"""Background workers feeding tracks into the anomaly engine.

Two long-running loops, started by the FastAPI lifespan:

  * ``FrameTrackerWorker`` — tails the per-camera Redis frame streams, decodes
    jpeg frames, runs ByteTrack (+ YOLOv8) to build geo-tracks, and evaluates
    each *closed* track (plus periodic active-track snapshots) through the engine
    with the exact frame as evidence. Inactive if ByteTrack deps are absent.

  * ``TelemetryWorker`` — tails the Kafka ``truck.telemetry`` topic, maintains a
    per-device GPS track (a bounded sliding window), and evaluates it through the
    engine on each update, fetching the device's assigned route for the
    route-deviation rule. Needs no heavy ML deps.

Both loops are fully fault-tolerant: a decode/detect/DB hiccup is logged and the
loop continues. They run blocking work (jpeg decode, YOLO inference, Kafka poll)
in worker threads so the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
from typing import List

import numpy as np

from jnpa_shared import kafka_io
from jnpa_shared.frame_bus import FrameBusConsumer
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TOPIC_TELEMETRY

from .config import AnomalyConfig
from .engine import AnomalyEngine
from .metrics import ACTIVE_TRACKS, ALERTS_RAISED, FRAMES_CONSUMED, TRACKS_PROCESSED
from .route_lookup import RouteCache
from .types import Track, TrackPoint

log = get_logger("anomaly.workers")

# Per-device telemetry window: keep the last N pings so dwell/route windows fit.
_TELEMETRY_WINDOW = 600
# Cap on tracked devices to bound memory (LRU eviction).
_MAX_DEVICES = 5000


def _count_alerts(alerts) -> None:
    for a in alerts:
        ALERTS_RAISED.labels(kind=a.kind, severity=a.severity).inc()


class FrameTrackerWorker:
    """Consumes the frame bus, runs ByteTrack, evaluates closed tracks."""

    name = "frame-tracker"

    def __init__(self, cfg: AnomalyConfig, engine: AnomalyEngine) -> None:
        self.cfg = cfg
        self.engine = engine
        self._stop = asyncio.Event()
        self._tracker = None
        self.active = False

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Lazy import so the service starts even without supervision/torch.
        try:
            from .track.bytetrack import VehicleTracker
        except Exception as exc:  # noqa: BLE001
            log.warning("bytetrack_import_failed", error=str(exc))
            return
        self._tracker = VehicleTracker(self.cfg)
        if not self._tracker.available:
            log.info("frame_tracker_inactive", reason="bytetrack_deps_missing")
            return
        self.active = True

        consumer = FrameBusConsumer(self.cfg.cameras, url=self.cfg.redis_url)
        log.info("frame_tracker_started", cameras=self.cfg.cameras)
        try:
            while not self._stop.is_set():
                msgs = await asyncio.to_thread(consumer.read)
                if not msgs:
                    continue
                for m in msgs:
                    FRAMES_CONSUMED.labels(camera_id=m.camera_id).inc()
                    await self._process_frame(m)
        except asyncio.CancelledError:
            pass
        finally:
            consumer.close()

    async def _process_frame(self, msg) -> None:
        import cv2

        arr = np.frombuffer(msg.jpeg, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if frame is None:
            return
        try:
            closed = await asyncio.to_thread(
                self._tracker.update, msg.camera_id, frame, msg.ts
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("track_update_failed", camera_id=msg.camera_id, error=str(exc))
            return
        ACTIVE_TRACKS.set(len(self._tracker.active_tracks()))
        for track in closed:
            TRACKS_PROCESSED.labels(source="bytetrack").inc()
            alerts = await asyncio.to_thread(
                self.engine.evaluate_track, track, jpeg=msg.jpeg
            )
            _count_alerts(alerts)


class TelemetryWorker:
    """Consumes truck.telemetry, builds per-device tracks, evaluates them."""

    name = "telemetry"

    def __init__(self, cfg: AnomalyConfig, engine: AnomalyEngine,
                 route_cache: RouteCache) -> None:
        self.cfg = cfg
        self.engine = engine
        self.route_cache = route_cache
        self._stop = asyncio.Event()
        self._tracks: "OrderedDict[str, Track]" = OrderedDict()
        self.active = True

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        import httpx

        log.info("telemetry_worker_started", topic=TOPIC_TELEMETRY)
        client = httpx.AsyncClient(timeout=self.cfg.truck_api_timeout_s)
        try:
            while not self._stop.is_set():
                batch = await asyncio.to_thread(self._poll_batch)
                if not batch:
                    continue
                for evt in batch:
                    await self._handle(evt, client)
        except asyncio.CancelledError:
            pass
        finally:
            await client.aclose()

    def _poll_batch(self) -> List[dict]:
        """Drain a small batch of telemetry events (blocking Kafka poll)."""
        out: List[dict] = []
        try:
            consumer = self._consumer()
            for _ in range(256):
                msg = consumer.poll(0.2)
                if msg is None:
                    break
                if msg.error():
                    continue
                out.append(kafka_io.decode_value(msg.value()))
        except Exception as exc:  # noqa: BLE001
            log.debug("telemetry_poll_failed", error=str(exc))
        return out

    def _consumer(self):
        if getattr(self, "_kc", None) is None:
            kc = kafka_io.get_consumer("anomaly-telemetry")
            kc.subscribe([TOPIC_TELEMETRY])
            self._kc = kc
        return self._kc

    async def _handle(self, evt: dict, client) -> None:
        device_id = evt.get("device_id")
        if not device_id:
            return
        track = self._tracks.get(device_id)
        if track is None:
            track = Track(track_id=f"TELE-{device_id}", device_id=device_id,
                          plate=evt.get("plate"))
            self._tracks[device_id] = track
            if len(self._tracks) > _MAX_DEVICES:
                self._tracks.popitem(last=False)
        self._tracks.move_to_end(device_id)

        ts = _parse_ts(evt.get("ts"))
        track.add(TrackPoint(
            ts=ts,
            lat=float(evt.get("lat", 0.0)),
            lon=float(evt.get("lon", 0.0)),
            speed_kmh=float(evt.get("speed_kmh", 0.0)),
            heading=float(evt.get("heading", 0.0)),
        ))
        if len(track.points) > _TELEMETRY_WINDOW:
            track.points = track.points[-_TELEMETRY_WINDOW:]

        route = await self.route_cache.fetch_route(device_id, client)
        TRACKS_PROCESSED.labels(source="telemetry").inc()
        alerts = await asyncio.to_thread(
            self.engine.evaluate_track, track, route=route
        )
        _count_alerts(alerts)


def _parse_ts(raw) -> datetime:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.now(tz=timezone.utc)


__all__ = ["FrameTrackerWorker", "TelemetryWorker"]
