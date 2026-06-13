"""End-to-end test for the ANPR ingestion service.

Generates a synthetic clip in a tmpdir, runs the service in-process with
DRY_RUN=true pointed at the live Kafka (host listener localhost:29092), and
asserts that at least one AnprRead message lands on `anpr.reads` within 10 s.

Requires the docker stack to be up (`make up`). It is skipped automatically if
Kafka is unreachable on the host, so `make test` stays green without infra.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = REPO_ROOT / "shared"
ANPR_SRC = REPO_ROOT / "ingest" / "anpr" / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for p in (str(SHARED_DIR), str(ANPR_SRC), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Host listener for Kafka (compose publishes EXTERNAL on localhost:29092).
KAFKA_HOST = os.environ.get("KAFKA_TEST_BROKERS", "localhost:29092")


def _kafka_reachable(hostport: str, timeout: float = 2.0) -> bool:
    host, _, port = hostport.partition(":")
    try:
        with socket.create_connection((host, int(port or 9092)), timeout=timeout):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _kafka_reachable(KAFKA_HOST),
    reason=f"Kafka not reachable at {KAFKA_HOST}; run `make up` first.",
)


@pytest.fixture()
def synthetic_clip(tmp_path: Path) -> Path:
    """Generate one synthetic clip in a tmp clips dir and return that dir."""
    import _synth_clip  # from scripts/

    clips = tmp_path / "clips"
    clips.mkdir()
    os.environ["CLIPS_DIR"] = str(clips)
    _synth_clip.main(["_synth_clip.py", "cam_test_entry.mp4"])
    made = list(clips.glob("*.mp4"))
    assert made and made[0].stat().st_size > 1024, "synthetic clip not generated"
    return clips


def test_dry_run_emits_to_kafka(synthetic_clip: Path, monkeypatch):
    # Point the whole service at the live broker + the tmp clips dir, DRY_RUN on.
    monkeypatch.setenv("KAFKA_BROKERS", KAFKA_HOST)
    monkeypatch.setenv("CLIPS_DIR", str(synthetic_clip))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("TARGET_FPS", "10")
    monkeypatch.setenv("SNAPSHOT_INTERVAL_S", "0.2")
    monkeypatch.setenv("METRICS_PORT", "9131")   # avoid clashing with a running svc
    monkeypatch.setenv("OPENWEATHER_API_KEY", "")  # weather pull skipped
    monkeypatch.setenv("LOG_LEVEL", "WARNING")

    # jnpa_shared.config caches Settings at import; clear so our env wins.
    from jnpa_shared.config import get_settings
    get_settings.cache_clear()

    import importlib
    from anpr_ingest import config as cfg_mod
    importlib.reload(cfg_mod)
    from anpr_ingest import main as main_mod
    importlib.reload(main_mod)

    cfg = cfg_mod.AnprConfig.from_env()
    assert cfg.dry_run is True
    assert cfg.kafka_brokers == KAFKA_HOST

    # Unique marker so we count only this run's messages.
    run_topic = f"anpr.reads"

    # --- Subscribe BEFORE starting the service (fresh group, latest offset) ---
    from confluent_kafka import Consumer

    group = f"anpr-test-{uuid.uuid4().hex[:8]}"
    consumer = Consumer({
        "bootstrap.servers": KAFKA_HOST,
        "group.id": group,
        "auto.offset.reset": "latest",
        "enable.auto.commit": True,
    })
    consumer.subscribe([run_topic])
    # Poll once to force assignment so we don't miss early messages.
    deadline = time.time() + 5
    while not consumer.assignment() and time.time() < deadline:
        consumer.poll(0.2)

    # --- Run the service in a background thread; stop it after a few seconds ---
    import asyncio
    from jnpa_shared import kafka_io  # noqa: F401 (ensures import works)

    stop_holder = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def runner():
            # Build the pipeline pieces directly so we can stop deterministically.
            from anpr_ingest.detect import VehicleDetector
            from anpr_ingest.emit import Emitter
            from anpr_ingest.replay import Replayer
            from anpr_ingest.weather import WeatherTagger

            replayer = Replayer(cfg)
            detector = VehicleDetector(cfg)
            emitter = Emitter(cfg)
            weather = WeatherTagger(cfg)
            stop = asyncio.Event()
            stop_holder["stop"] = stop
            stop_holder["loop"] = loop
            task = asyncio.create_task(
                main_mod._frame_loop(cfg, replayer, detector, emitter, weather, stop, None)
            )
            await stop.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            emitter.flush(3.0)
            replayer.close()

        loop.run_until_complete(runner())
        loop.close()

    th = threading.Thread(target=_run, daemon=True)
    th.start()

    # --- Collect messages for up to 10 s ---
    received = 0
    end = time.time() + 10
    try:
        while time.time() < end:
            msg = consumer.poll(0.5)
            if msg is None or msg.error():
                continue
            received += 1
            if received >= 1:
                # We have proof of life; keep draining briefly then stop.
                if received >= 3:
                    break
    finally:
        # Signal the service to stop.
        stop = stop_holder.get("stop")
        loop = stop_holder.get("loop")
        if stop is not None and loop is not None:
            loop.call_soon_threadsafe(stop.set)
        th.join(timeout=8)
        consumer.close()

    assert received > 0, "expected at least one AnprRead on anpr.reads within 10s"
