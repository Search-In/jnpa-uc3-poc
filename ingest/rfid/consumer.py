"""RFID MQTT consumer — JNPA UC-III PoC.

Subscribes to ``rfid/readers/+``, validates each payload against the ``RfidRead``
schema, writes valid reads to ``core.rfid_read`` (Timescale), and forwards them
to the Kafka topic ``rfid.reads`` for downstream consumers (the correlator).

Architecture: paho runs its network loop on a background thread and pushes
decoded reads onto a thread-safe queue. A single asyncio writer task drains the
queue, batches the Timescale inserts, and produces to Kafka. This keeps the MQTT
callback fast and the DB writes off the network thread.

Resilient to broker restart (MQTT auto-reconnect + re-subscribe on connect) and
to transient DB/Kafka hiccups (failed batches are retried, never dropped silently
beyond a bounded queue).

Run with ``rfid-consumer`` or ``python -m rfid_ingest.consumer``.
"""
from __future__ import annotations

import asyncio
import queue
import signal
import threading
from typing import List, Optional

import asyncpg
from pydantic import ValidationError

from jnpa_shared import kafka_io
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import RfidRead

from rfid_ingest import mqtt_io
from rfid_ingest.config import MQTT_TOPIC_WILDCARD, RfidConfig
from rfid_ingest.metrics import (
    KAFKA_ERRORS,
    RFID_CONSUMED,
    RFID_FORWARDED,
    RFID_PERSISTED,
    RFID_VALIDATION_ERRORS,
    counter_total,
    start_metrics_server,
)

log = get_logger("rfid_ingest.consumer")

_INSERT_SQL = (
    "INSERT INTO core.rfid_read (ts, reader_id, tag_id, rssi) "
    "VALUES ($1, $2, $3, $4)"
)


class Consumer:
    """MQTT -> Timescale + Kafka bridge."""

    def __init__(self, cfg: RfidConfig) -> None:
        self.cfg = cfg
        # Bounded so a stalled DB applies backpressure instead of OOM-ing.
        self._q: "queue.Queue[RfidRead]" = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._producer = kafka_io.get_producer(
            {"bootstrap.servers": cfg.kafka_brokers, "client.id": "rfid-consumer"}
        )
        self.client = mqtt_io.build_client(
            cfg,
            client_id="rfid-consumer",
            on_connect=self._on_connect,
            on_message=self._on_message,
        )

    # -- MQTT callbacks (background network thread) -------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        # (Re)subscribe on every (re)connect so a broker restart self-heals.
        client.subscribe(MQTT_TOPIC_WILDCARD, qos=self.cfg.mqtt_qos)
        log.info("mqtt_subscribed", topic=MQTT_TOPIC_WILDCARD, reason=str(reason_code))

    def _on_message(self, client, userdata, msg):
        RFID_CONSUMED.inc()
        try:
            read = RfidRead.model_validate_json(msg.payload)
        except ValidationError as exc:
            RFID_VALIDATION_ERRORS.inc()
            log.warning("rfid_validation_failed", topic=msg.topic, error=str(exc))
            return
        try:
            self._q.put_nowait(read)
        except queue.Full:
            # Drop oldest by making room — better than blocking the net thread.
            try:
                self._q.get_nowait()
                self._q.put_nowait(read)
            except queue.Empty:
                pass
            log.warning("rfid_queue_full_dropped_oldest")

    # -- Kafka forward ------------------------------------------------------
    def _forward(self, read: RfidRead) -> None:
        try:
            kafka_io.produce(
                self._producer, self.cfg.rfid_topic, read, key=read.reader_id, flush=False,
                event_type="jnpa.rfid.read",
                source_system="SIM",     # reads originate from the RFID emulator
                raw_ref=f"reader://{read.reader_id}#tag={read.tag_id}",
            )
            self._producer.poll(0)
            RFID_FORWARDED.inc()
        except Exception as exc:  # noqa: BLE001
            KAFKA_ERRORS.inc()
            log.warning("kafka_forward_failed", error=str(exc), reader_id=read.reader_id)

    # -- async DB writer ----------------------------------------------------
    async def _writer(self) -> None:
        pool = await self._connect_pool()
        try:
            while not self._stop.is_set() or not self._q.empty():
                batch = self._drain_batch(max_n=500)
                if not batch:
                    await asyncio.sleep(0.1)
                    continue
                await self._persist_batch(pool, batch)
                for read in batch:
                    self._forward(read)
                self._producer.poll(0)
        finally:
            try:
                self._producer.flush(5)
            except Exception:  # noqa: BLE001
                pass
            await pool.close()

    async def _connect_pool(self) -> asyncpg.Pool:
        """Create the asyncpg pool, retrying until Postgres is reachable."""
        delay = 1.0
        while not self._stop.is_set():
            try:
                pool = await asyncpg.create_pool(
                    dsn=self.cfg.postgres_dsn, min_size=1, max_size=4
                )
                log.info("postgres_connected")
                return pool
            except Exception as exc:  # noqa: BLE001
                log.warning("postgres_connect_retry", error=str(exc), delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise RuntimeError("stopped before postgres connected")

    def _drain_batch(self, max_n: int) -> List[RfidRead]:
        batch: List[RfidRead] = []
        for _ in range(max_n):
            try:
                batch.append(self._q.get_nowait())
            except queue.Empty:
                break
        return batch

    async def _persist_batch(self, pool: asyncpg.Pool, batch: List[RfidRead]) -> None:
        rows = [(r.ts, r.reader_id, r.tag_id, r.rssi) for r in batch]
        delay = 0.5
        while not self._stop.is_set():
            try:
                async with pool.acquire() as conn:
                    await conn.executemany(_INSERT_SQL, rows)
                RFID_PERSISTED.inc(len(rows))
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("rfid_persist_retry", error=str(exc), n=len(rows), delay=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    async def _stats(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(5.0)
            log.info(
                "consumer_stats",
                consumed=int(counter_total(RFID_CONSUMED)),
                persisted=int(counter_total(RFID_PERSISTED)),
                forwarded=int(counter_total(RFID_FORWARDED)),
                invalid=int(counter_total(RFID_VALIDATION_ERRORS)),
                queue=self._q.qsize(),
            )

    # -- lifecycle ----------------------------------------------------------
    async def run_async(self) -> None:
        mqtt_io.start(self.cfg, self.client)
        log.info("consumer_started", kafka=self.cfg.kafka_brokers, dsn=self.cfg.postgres_dsn)
        writer = asyncio.create_task(self._writer(), name="writer")
        stats = asyncio.create_task(self._stats(), name="stats")
        try:
            # Idle until stop; the writer/stats tasks do the work.
            while not self._stop.is_set():
                await asyncio.sleep(0.2)
        finally:
            self._stop.set()
            await asyncio.gather(writer, stats, return_exceptions=True)
            mqtt_io.stop(self.client)
            log.info("consumer_stopped")

    def request_stop(self) -> None:
        self._stop.set()


def run() -> None:
    cfg = RfidConfig.from_env()
    configure_logging(cfg.log_level)
    start_metrics_server(cfg.metrics_port)
    consumer = Consumer(cfg)

    async def _main() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, consumer.request_stop)
            except (NotImplementedError, ValueError):
                pass
        await consumer.run_async()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
