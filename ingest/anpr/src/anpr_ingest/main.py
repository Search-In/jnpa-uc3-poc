"""anpr-ingest entrypoint: a long-running asyncio service.

Pipeline per replayed frame:
    replay -> detect (YOLOv8n) -> [DRY_RUN: raw crop | else: AI OCR] -> Kafka

Concurrently:
    * weather refresher (every 10 min) tags frames fog/rain/dust/clear
    * no_feed watchdog emits a health event every 5 s while zero clips exist
    * Prometheus /metrics server on the configured port

Run with `python -m anpr_ingest.main` or the `anpr-ingest` console script.
"""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Dict, Optional

import cv2
import httpx

from jnpa_shared.frame_bus import FrameBusProducer
from jnpa_shared.logging import configure_logging, get_logger

from .config import AnprConfig, validate_anpr_config
from .detect import VehicleDetector
from .emit import Emitter
from .metrics import (
    ACTIVE_FEEDS,
    FRAMES_PROCESSED,
    FRAMES_PUBLISHED,
    snapshot as metrics_snapshot,
    start_metrics_server,
)
from .replay import Replayer
from .weather import WeatherTagger


def _publish_frame(
    bus: Optional[FrameBusProducer], cfg: AnprConfig, camera_id: str, frame, ts
) -> None:
    """JPEG-encode and mirror one frame onto the shared Redis frame bus.

    Runs inside a worker thread (alongside detection) so the encode stays off the
    event loop. Best-effort: any failure is swallowed by the producer.
    """
    if bus is None:
        return
    ok, buf = cv2.imencode(
        ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), cfg.frame_jpeg_quality]
    )
    if not ok:
        return
    if bus.publish(camera_id, buf.tobytes(), ts) is not None:
        FRAMES_PUBLISHED.labels(camera_id=camera_id).inc()


async def _frame_loop(
    cfg: AnprConfig,
    replayer: Replayer,
    detector: VehicleDetector,
    emitter: Emitter,
    weather: WeatherTagger,
    frame_bus: Optional[FrameBusProducer],
    stop: asyncio.Event,
    log,
) -> None:
    """Consume replayed frames and emit AnprRead events until `stop` is set."""
    # Per-camera last-snapshot timestamp for the per-second snapshot cadence.
    last_snapshot: Dict[str, float] = {}
    client = httpx.AsyncClient() if not cfg.dry_run else None
    try:
        async for camera_id, frame, ts in replayer.frames():
            if stop.is_set():
                break

            ACTIVE_FEEDS.set(len(replayer.feeds))
            FRAMES_PROCESSED.labels(camera_id=camera_id).inc()

            # Per-second snapshot throttle: detect at most ~once/sec/camera, so
            # we deliver clean per-second snapshots rather than every frame.
            now = time.monotonic()
            prev = last_snapshot.get(camera_id, 0.0)
            do_snapshot = (now - prev) >= cfg.snapshot_interval_s
            if not do_snapshot:
                continue
            last_snapshot[camera_id] = now

            # Mirror the sampled frame onto the shared bus for ai/anomaly et al.
            # (jpeg encode runs in the worker thread, off the event loop).
            if frame_bus is not None:
                await asyncio.to_thread(
                    _publish_frame, frame_bus, cfg, camera_id, frame, ts
                )

            # Detection is CPU-bound; run it off the event loop.
            candidates = await asyncio.to_thread(detector.detect, camera_id, frame)
            wx = weather.current()
            condition = weather.condition(ts)

            for cand in candidates:
                if cfg.dry_run:
                    emitter.emit_dry_run(cand, ts, wx, condition)
                else:
                    assert client is not None
                    await emitter.emit_with_ai(cand, ts, wx, client)

            emitter.flush(timeout=1.0)
    finally:
        if client is not None:
            await client.aclose()


async def _no_feed_loop(
    cfg: AnprConfig, replayer: Replayer, emitter: Emitter, stop: asyncio.Event, log
) -> None:
    """Emit a no_feed health event every no_feed_interval_s while zero clips exist."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=cfg.no_feed_interval_s)
            break
        except asyncio.TimeoutError:
            pass
        if not replayer.refresh_feeds():
            ACTIVE_FEEDS.set(0)
            emitter.emit_no_feed()
            emitter.flush(timeout=1.0)


async def _stats_loop(stop: asyncio.Event, log, interval_s: float = 5.0) -> None:
    """Periodically log current metric totals so they are greppable in the logs
    (the /metrics endpoint always has the authoritative values)."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
            break
        except asyncio.TimeoutError:
            pass
        log.info("anpr_stats", **metrics_snapshot())


async def main_async() -> None:
    cfg = AnprConfig.from_env()
    configure_logging(cfg.log_level)
    log = get_logger("anpr_ingest.main")
    # Fail-fast before any work: never run synthetic OCR in a production-like env.
    validate_anpr_config(cfg)
    log.info(
        "anpr_ingest_starting",
        dry_run=cfg.dry_run,
        clips_dir=cfg.clips_dir,
        kafka=cfg.kafka_brokers,
        topic=cfg.topic,
        metrics_port=cfg.metrics_port,
    )

    start_metrics_server(cfg.metrics_port)

    replayer = Replayer(cfg)
    detector = VehicleDetector(cfg)
    emitter = Emitter(cfg)
    weather = WeatherTagger(cfg)
    frame_bus = (
        FrameBusProducer(url=cfg.redis_url, maxlen=cfg.frame_bus_maxlen)
        if cfg.publish_frames
        else None
    )
    if frame_bus is not None:
        log.info("frame_bus_enabled", redis=cfg.redis_url, maxlen=cfg.frame_bus_maxlen)

    stop = asyncio.Event()

    def _request_stop(*_a) -> None:
        log.info("shutdown_signal_received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, ValueError):
            # Signal handlers are unavailable off the main thread (e.g. tests).
            pass

    tasks = [
        asyncio.create_task(weather.run(stop), name="weather"),
        asyncio.create_task(
            _frame_loop(cfg, replayer, detector, emitter, weather, frame_bus, stop, log),
            name="frames"),
        asyncio.create_task(_no_feed_loop(cfg, replayer, emitter, stop, log), name="no_feed"),
        asyncio.create_task(_stats_loop(stop, log, cfg.no_feed_interval_s), name="stats"),
    ]
    try:
        await stop.wait()
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        emitter.flush()
        replayer.close()
        if frame_bus is not None:
            frame_bus.close()
        log.info("anpr_ingest_stopped")


def run() -> None:
    """Console-script / module entrypoint."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
