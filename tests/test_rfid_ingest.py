"""End-to-end tests for the RFID emulator / consumer / correlator.

Run the services in-process against the live docker stack (host listeners:
MQTT localhost:1883, Kafka localhost:29092, Postgres localhost:5433). Skipped
automatically if any of those is unreachable, so `make test` stays green
without infra.

Tests
  1. Start emulator + consumer for ~30 s, assert >= 50 rows land in
     core.rfid_read.
  2. Inject one synthetic RFID tag + a matching ANPR plate at the same gate and
     assert a vehicle.confirmed message arrives within 6 s.
"""
from __future__ import annotations

import json
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
RFID_DIR = REPO_ROOT / "ingest" / "rfid"
for p in (str(SHARED_DIR), str(RFID_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Host listeners (compose publishes these on localhost).
KAFKA_HOST = os.environ.get("KAFKA_TEST_BROKERS", "localhost:29092")
MQTT_HOST = os.environ.get("MQTT_TEST_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_TEST_PORT", "1883"))
PG_HOST = os.environ.get("PG_TEST_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_TEST_PORT", "5433"))
PG_DSN = os.environ.get(
    "RFID_TEST_DSN", f"postgresql://postgres:jnpa_pw@{PG_HOST}:{PG_PORT}/postgres"
)


def _reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_INFRA_UP = (
    _reachable(*KAFKA_HOST.split(":")[0:1] + [int(KAFKA_HOST.split(":")[1])])
    and _reachable(MQTT_HOST, MQTT_PORT)
    and _reachable(PG_HOST, PG_PORT)
)

pytestmark = pytest.mark.skipif(
    not _INFRA_UP,
    reason=(
        f"RFID infra not reachable (Kafka {KAFKA_HOST}, MQTT {MQTT_HOST}:{MQTT_PORT}, "
        f"Postgres {PG_HOST}:{PG_PORT}); run `make up` first."
    ),
)


def _test_env(monkeypatch):
    """Point the shared + rfid config at the host listeners."""
    monkeypatch.setenv("KAFKA_BROKERS", KAFKA_HOST)
    monkeypatch.setenv("MQTT_HOST", MQTT_HOST)
    monkeypatch.setenv("MQTT_PORT", str(MQTT_PORT))
    monkeypatch.setenv("POSTGRES_DSN_LIBPQ", PG_DSN)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    from jnpa_shared.config import get_settings

    get_settings.cache_clear()


def _fresh_config():
    import importlib
    from rfid_ingest import config as cfg_mod

    importlib.reload(cfg_mod)
    return cfg_mod.RfidConfig.from_env()


def test_emulator_consumer_land_rows(monkeypatch):
    """30 s of emulator + consumer should land >= 50 rfid_reads rows."""
    _test_env(monkeypatch)
    # Crank the rate up so 30 s comfortably clears 50 rows even off-peak.
    monkeypatch.setenv("RFID_BASE_RATE", "1.0")
    monkeypatch.setenv("METRICS_PORT", "0")  # don't bind a metrics port in-test
    cfg = _fresh_config()

    import asyncpg
    import asyncio

    # Baseline row count so the assertion counts only this run.
    async def _count() -> int:
        conn = await asyncpg.connect(dsn=PG_DSN)
        try:
            return await conn.fetchval("SELECT count(*) FROM core.rfid_read")
        finally:
            await conn.close()

    before = asyncio.run(_count())

    from emulator import Emulator
    from consumer import Consumer

    emu = Emulator(cfg)
    con = Consumer(cfg)

    # Consumer runs its own asyncio loop on a thread.
    def _run_consumer():
        asyncio.run(con.run_async())

    emu_th = threading.Thread(target=emu.run, daemon=True)
    con_th = threading.Thread(target=_run_consumer, daemon=True)
    con_th.start()
    time.sleep(1.0)  # let the consumer subscribe before reads start
    emu_th.start()

    try:
        # Spec: run ~30 s. Poll the row delta so we can finish early once we
        # clearly pass the threshold.
        deadline = time.time() + 35
        delta = 0
        while time.time() < deadline:
            time.sleep(3.0)
            delta = asyncio.run(_count()) - before
            if delta >= 50 and time.time() > (deadline - 35) + 30:
                break
    finally:
        emu.request_stop()
        con.request_stop()
        emu_th.join(timeout=5)
        con_th.join(timeout=8)

    after = asyncio.run(_count())
    landed = after - before
    assert landed >= 50, f"expected >= 50 rfid_reads rows in ~30 s, got {landed}"


def test_correlator_emits_vehicle_confirmed(monkeypatch):
    """Inject a matching RFID tag + ANPR plate at one gate; confirm within 6 s."""
    _test_env(monkeypatch)
    monkeypatch.setenv("METRICS_PORT", "0")
    # Unique consumer group so this in-test correlator reads its own copy of the
    # streams and does NOT share offsets with a running rfid-correlator container
    # (which would otherwise steal the injected messages).
    monkeypatch.setenv("RFID_CORRELATOR_GROUP", f"rfid-correlator-test-{uuid.uuid4().hex[:8]}")
    cfg = _fresh_config()

    from confluent_kafka import Consumer as KConsumer
    from jnpa_shared import kafka_io
    from correlator import Correlator

    gate = "G-NSICT"
    camera_id = "CAM-NSICT-ENT"   # belongs to G-NSICT (core.camera seed)
    reader_id = "R-01"            # gate reader at G-NSICT (topology)
    plate = "MH04AB1234"
    tag = "E2801160" + uuid.uuid4().hex[:16].upper()

    # --- Subscribe to vehicle.confirmed BEFORE we inject (latest offset) ---
    group = f"rfid-confirm-test-{uuid.uuid4().hex[:8]}"
    kc = KConsumer(
        {
            "bootstrap.servers": KAFKA_HOST,
            "group.id": group,
            "auto.offset.reset": "latest",
            "enable.auto.commit": True,
        }
    )
    kc.subscribe([cfg.confirmed_topic])
    deadline = time.time() + 5
    while not kc.assignment() and time.time() < deadline:
        kc.poll(0.2)

    # --- Start the correlator on a background thread ---
    corr = Correlator(cfg)
    corr_th = threading.Thread(target=corr.run, daemon=True)
    corr_th.start()
    time.sleep(2.0)  # let its rfid/anpr consumers join and seek to latest

    now_iso = __import__("datetime").datetime.now(
        tz=__import__("datetime").timezone.utc
    ).isoformat()

    prod = kafka_io.get_producer({"bootstrap.servers": KAFKA_HOST, "client.id": "rfid-test"})
    rfid_msg = {"ts": now_iso, "reader_id": reader_id, "tag_id": tag, "rssi": -42.3}
    anpr_msg = {
        "ts": now_iso,
        "camera_id": camera_id,
        "plate": plate,
        "conf": 0.95,
        "vehicle_class": "HGV",
    }
    kafka_io.produce(prod, cfg.rfid_topic, rfid_msg, key=reader_id, flush=False)
    kafka_io.produce(prod, cfg.anpr_topic, anpr_msg, key=camera_id, flush=True)

    # --- Expect a vehicle.confirmed within 6 s ---
    matched = None
    try:
        end = time.time() + 6
        while time.time() < end:
            msg = kc.poll(0.5)
            if msg is None or msg.error():
                continue
            event = json.loads(msg.value())
            if event.get("gate_id") == gate and event.get("rfid_tag") == tag:
                matched = event
                break
    finally:
        corr.request_stop()
        corr_th.join(timeout=5)
        kc.close()

    assert matched is not None, "no matching vehicle.confirmed within 6 s"
    assert matched["plate"] == plate
    assert matched["camera_id"] == camera_id
    assert matched["confidence"] == pytest.approx(cfg.correlator_confidence)
