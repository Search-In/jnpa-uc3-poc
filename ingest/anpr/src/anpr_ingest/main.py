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
from typing import Dict

import httpx

from jnpa_shared.logging import configure_logging, get_logger

from .config import AnprConfig
from .detect import VehicleDetector
from .emit import Emitter
from .metrics import (
    ACTIVE_FEEDS,
    FRAMES_PROCESSED,
    snapshot as metrics_snapshot,
    start_metrics_server,
)
from .replay import Replayer
from .weather import WeatherTagger


async def _frame_loop(
    cfg: AnprConfig,
    replayer: Replayer,
    detector: VehicleDetector,
    emitter: Emitter,
    weather: WeatherTagger,
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

            # Detection is CPU-bound; run it off the event loop.
            candidates = await asyncio.to_thread(detector.detect, camera_id, frame)
            wx = weather.current()

            for cand in candidates:
                if cfg.dry_run:
                    emitter.emit_dry_run(cand, ts, wx)
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
        asyncio.create_task(_frame_loop(cfg, replayer, detector, emitter, weather, stop, log),
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
        log.info("anpr_ingest_stopped")


def run() -> None:
    """Console-script / module entrypoint."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
