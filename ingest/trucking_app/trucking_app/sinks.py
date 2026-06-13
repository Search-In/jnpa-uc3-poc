"""Output sinks: MQTT (aiomqtt), Kafka, and a batched-COPY Timescale writer.

Three independent, fault-tolerant sinks fed by the simulator:

  * ``MqttSink`` — one resilient aiomqtt connection. Position telemetry is
    published with QoS 0 (high rate, lossy-OK); state changes and ETAs with QoS
    1. A broker outage is absorbed by an auto-reconnect loop; publishes made
    while down are dropped (QoS 0) without crashing the producer.
  * ``KafkaSink`` — wraps the shared confluent-kafka producer; non-blocking
    ``produce`` + periodic ``poll`` so delivery callbacks fire. JSON+snappy.
  * ``DbSink`` — buffers ``TruckTelemetry`` rows and flushes them to
    ``jnpa.truck_telemetry`` with asyncpg ``copy_records_to_table`` (binary COPY)
    every ``db_flush_interval_s`` — the high-throughput write path the spec asks
    for. Postgres connect is retried; failed flushes are retried, never silently
    dropped beyond a bounded buffer.

Every sink is best-effort and isolated: an error in one never stops the others
or the simulation loop.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import aiomqtt
import asyncpg

from jnpa_shared import kafka_io
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TruckTelemetry

from .config import (
    MQTT_ETA_SUFFIX,
    MQTT_TELEMETRY_PREFIX,
    MQTT_TELEMETRY_SUFFIX,
    TELEMETRY_TABLE,
    TruckConfig,
)
from .metrics import (
    DB_QUEUE_DEPTH,
    ETA_PUBLISHED,
    PUBLISH_ERRORS,
    TELEMETRY_PERSISTED,
    TELEMETRY_PUBLISHED,
)

log = get_logger("trucking_app.sinks")


# ===========================================================================
# MQTT
# ===========================================================================
class MqttSink:
    """Resilient aiomqtt publisher (auto-reconnect, QoS-aware)."""

    def __init__(self, cfg: TruckConfig) -> None:
        self.cfg = cfg
        self._client: Optional[aiomqtt.Client] = None
        self._connected = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        # paho (under aiomqtt) logs a WARNING per in-flight QoS-0 publish ("There
        # are N pending publish calls."). At 4,000 msg/s that floods stdout and
        # says nothing actionable — the connection loop already reports real
        # drops. Quiet it to ERROR.
        logging.getLogger("mqtt").setLevel(logging.ERROR)
        self._task = asyncio.create_task(self._connection_loop(), name="mqtt-conn")
        # Give the first connection a moment, but don't block startup on the broker.
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("mqtt_initial_connect_pending", host=self.cfg.mqtt_host)

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _connection_loop(self) -> None:
        """Hold a live connection, reconnecting with backoff on any drop."""
        delay = 1.0
        while not self._stop.is_set():
            try:
                async with aiomqtt.Client(
                    hostname=self.cfg.mqtt_host,
                    port=self.cfg.mqtt_port,
                    keepalive=self.cfg.mqtt_keepalive,
                    identifier="truck-sim",
                ) as client:
                    self._client = client
                    self._connected.set()
                    log.info("mqtt_connected", host=self.cfg.mqtt_host, port=self.cfg.mqtt_port)
                    delay = 1.0
                    # Stay connected until asked to stop.
                    while not self._stop.is_set():
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - reconnect on any error
                self._connected.clear()
                self._client = None
                log.warning("mqtt_disconnected", error=str(exc), retry_in=delay)
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
                delay = min(delay * 2, self.cfg.mqtt_reconnect_max_s)
        self._connected.clear()
        self._client = None

    async def publish_telemetry(self, device_id: str, payload: str) -> None:
        topic = f"{MQTT_TELEMETRY_PREFIX}/{device_id}/{MQTT_TELEMETRY_SUFFIX}"
        await self._publish(topic, payload, qos=self.cfg.mqtt_qos_position, kind="telemetry")

    async def publish_eta(self, device_id: str, payload: str) -> None:
        topic = f"{MQTT_TELEMETRY_PREFIX}/{device_id}/{MQTT_ETA_SUFFIX}"
        await self._publish(topic, payload, qos=self.cfg.mqtt_qos_state, kind="eta")

    async def _publish(self, topic: str, payload: str, qos: int, kind: str) -> None:
        client = self._client
        if client is None or not self._connected.is_set():
            PUBLISH_ERRORS.labels("mqtt").inc()
            return  # disconnected: drop (QoS0 semantics) without raising
        try:
            await client.publish(topic, payload=payload, qos=qos)
            if kind == "eta":
                ETA_PUBLISHED.labels("mqtt").inc()
            else:
                TELEMETRY_PUBLISHED.labels("mqtt").inc()
        except Exception as exc:  # noqa: BLE001
            PUBLISH_ERRORS.labels("mqtt").inc()
            log.debug("mqtt_publish_failed", topic=topic, error=str(exc))


# ===========================================================================
# Kafka
# ===========================================================================
class KafkaSink:
    """Non-blocking confluent-kafka producer for analytics topics."""

    def __init__(self, cfg: TruckConfig) -> None:
        self.cfg = cfg
        self._producer = kafka_io.get_producer(
            {
                "bootstrap.servers": cfg.kafka_brokers,
                "client.id": "truck-sim",
                # High-throughput: bigger batches, snappy already set by shared.
                "linger.ms": 50,
                "queue.buffering.max.messages": 1_000_000,
            }
        )

    def publish_telemetry(self, device_id: str, event: TruckTelemetry) -> None:
        self._produce(self.cfg.telemetry_topic, device_id, event, kind="telemetry")

    def publish_eta(self, device_id: str, payload: dict) -> None:
        self._produce(self.cfg.eta_topic, device_id, payload, kind="eta")

    def _produce(self, topic: str, key: str, value, kind: str) -> None:
        try:
            kafka_io.produce(self._producer, topic, value, key=key, flush=False)
            if kind == "eta":
                ETA_PUBLISHED.labels("kafka").inc()
            else:
                TELEMETRY_PUBLISHED.labels("kafka").inc()
        except BufferError:
            # Local queue full: drain delivery callbacks and count, don't crash.
            self._producer.poll(0)
            PUBLISH_ERRORS.labels("kafka").inc()
        except Exception as exc:  # noqa: BLE001
            PUBLISH_ERRORS.labels("kafka").inc()
            log.debug("kafka_produce_failed", topic=topic, error=str(exc))

    def poll(self) -> None:
        self._producer.poll(0)

    def flush(self, timeout: float = 5.0) -> None:
        try:
            self._producer.flush(timeout)
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# Timescale (batched COPY)
# ===========================================================================
_COPY_COLUMNS = (
    "ts", "device_id", "plate", "lat", "lon",
    "speed_kmh", "heading", "battery", "accuracy_m",
)


class DbSink:
    """Buffers telemetry and flushes via asyncpg binary COPY every N seconds."""

    def __init__(self, cfg: TruckConfig) -> None:
        self.cfg = cfg
        self._buf: List[tuple] = []
        self._lock = asyncio.Lock()
        self._pool: Optional[asyncpg.Pool] = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._pool = await self._connect_pool()

    async def close(self) -> None:
        self._stop.set()
        # Final flush so in-flight rows aren't lost on shutdown.
        await self.flush()
        if self._pool is not None:
            await self._pool.close()

    def enqueue(self, event: TruckTelemetry) -> None:
        """Append a row to the buffer (called from the hot loop; cheap)."""
        self._buf.append(
            (
                event.ts,
                event.device_id,
                event.plate,
                event.lat,
                event.lon,
                event.speed_kmh,
                event.heading,
                event.battery,
                event.accuracy_m,
            )
        )
        DB_QUEUE_DEPTH.set(len(self._buf))
        # Bound the buffer: if the DB stalls, shed the oldest rows rather than OOM.
        if len(self._buf) > self.cfg.db_batch_max * 4:
            drop = len(self._buf) - self.cfg.db_batch_max * 4
            del self._buf[:drop]
            PUBLISH_ERRORS.labels("db").inc()
            log.warning("db_buffer_overflow_dropped", dropped=drop)

    async def flush(self) -> int:
        """COPY the buffered rows to Timescale. Returns rows written."""
        if self._pool is None:
            return 0
        async with self._lock:
            if not self._buf:
                return 0
            rows = self._buf
            self._buf = []
        DB_QUEUE_DEPTH.set(0)
        written = await self._copy(rows)
        return written

    async def _copy(self, rows: List[tuple]) -> int:
        assert self._pool is not None
        delay = 0.5
        attempts = 0
        while not self._stop.is_set() or attempts == 0:
            try:
                async with self._pool.acquire() as conn:
                    await conn.copy_records_to_table(
                        "truck_telemetry",
                        schema_name="jnpa",
                        columns=_COPY_COLUMNS,
                        records=rows,
                    )
                TELEMETRY_PERSISTED.inc(len(rows))
                return len(rows)
            except Exception as exc:  # noqa: BLE001
                attempts += 1
                PUBLISH_ERRORS.labels("db").inc()
                log.warning("db_copy_retry", error=str(exc), n=len(rows), delay=delay)
                if attempts >= 3:
                    # Re-buffer and give up this round; next flush retries.
                    async with self._lock:
                        self._buf = rows + self._buf
                    return 0
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)
        return 0

    async def _connect_pool(self) -> asyncpg.Pool:
        delay = 1.0
        while not self._stop.is_set():
            try:
                pool = await asyncpg.create_pool(
                    dsn=self.cfg.postgres_dsn,
                    min_size=self.cfg.db_pool_min,
                    max_size=self.cfg.db_pool_max,
                )
                log.info("postgres_connected")
                return pool
            except Exception as exc:  # noqa: BLE001
                log.warning("postgres_connect_retry", error=str(exc), delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("stopped before postgres connected")
