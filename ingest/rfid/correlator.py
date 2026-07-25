"""RFID <-> ANPR correlator — JNPA UC-III PoC.

A background job that joins ``rfid.reads`` with ``anpr.reads`` inside a
5-second window *per gate*. When an RFID tag read and an ANPR plate read land
at the same gate within the window, it emits a confirmed-vehicle event to the
Kafka topic ``vehicle.confirmed``:

    {"ts", "plate", "rfid_tag", "camera_id", "gate_id", "confidence": 0.97}

This confirmed event is what feeds the boom-barrier decision and the
gate-throughput KPI on the dashboard.

Gate resolution:
  * ANPR camera_id -> gate_id is loaded from ``core.camera`` (falls back to a
    static map if the DB is unavailable).
  * RFID reader_id -> gate_id comes from the emulator topology (only the gate
    readers participate; corridor reads have no gate and are ignored for the
    join — they still flow to Timescale via the consumer).

Two confluent-kafka consumers (one per topic) run on background threads and push
reads into a per-gate, time-windowed buffer. A matcher pairs the freshest tag
with the freshest plate at each gate within the window and emits once per pair.

Run with ``rfid-correlator`` or ``python -m rfid_ingest.correlator``.
"""
from __future__ import annotations

import signal
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, Dict, Optional, Tuple

from jnpa_shared import kafka_io
from jnpa_shared.logging import configure_logging, get_logger

from rfid_ingest.config import RfidConfig
from rfid_ingest.metrics import (
    ANPR_SEEN,
    KAFKA_ERRORS,
    RFID_SEEN,
    VEHICLE_CONFIRMED,
    counter_total,
    start_metrics_server,
)
from rfid_ingest.topology import build_readers, reader_gate_map

log = get_logger("rfid_ingest.correlator")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# Static camera->gate fallback (mirrors core.camera seed rows). Used if the DB
# lookup fails so the correlator still works without Postgres.
_STATIC_CAMERA_GATE = {
    "CAM-NSICT-ENT": "G-NSICT", "CAM-NSICT-EXT": "G-NSICT", "CAM-NSICT-OVW": "G-NSICT",
    "CAM-JNPCT-ENT": "G-JNPCT", "CAM-JNPCT-EXT": "G-JNPCT", "CAM-JNPCT-OVW": "G-JNPCT",
    "CAM-NSIGT-ENT": "G-NSIGT", "CAM-NSIGT-EXT": "G-NSIGT", "CAM-NSIGT-OVW": "G-NSIGT",
    "CAM-BMCT-ENT": "G-BMCT", "CAM-BMCT-EXT": "G-BMCT", "CAM-BMCT-OVW": "G-BMCT",
}


@dataclass
class _Buffered:
    """A read held in the per-gate window (value is plate or tag)."""

    value: str
    extra: str       # camera_id (for plate) or reader_id (for tag)
    mono: float      # monotonic receive time, for window expiry
    ts: datetime     # event timestamp from the payload


class Correlator:
    def __init__(self, cfg: RfidConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Per gate: recent plate reads and recent tag reads (newest at the right).
        self._plates: Dict[str, Deque[_Buffered]] = defaultdict(deque)
        self._tags: Dict[str, Deque[_Buffered]] = defaultdict(deque)
        # Dedup: a (gate, plate, tag) we have already confirmed within a window.
        self._emitted: Dict[Tuple[str, str, str], float] = {}
        self._producer = kafka_io.get_producer(
            {"bootstrap.servers": cfg.kafka_brokers, "client.id": "rfid-correlator"}
        )
        self._reader_gate = reader_gate_map(
            build_readers(cfg.num_gate_readers, cfg.num_corridor_readers)
        )
        self._camera_gate: Dict[str, str] = dict(_STATIC_CAMERA_GATE)

    # -- gate resolution ----------------------------------------------------
    def _load_camera_gate_map(self) -> None:
        """Best-effort: load camera->gate from Postgres, fall back to static."""
        try:
            import asyncpg
            import asyncio

            async def _load():
                conn = await asyncpg.connect(dsn=self.cfg.postgres_dsn)
                try:
                    rows = await conn.fetch(
                        "SELECT id, gate_id FROM core.camera WHERE gate_id IS NOT NULL"
                    )
                    return {r["id"]: r["gate_id"] for r in rows}
                finally:
                    await conn.close()

            loaded = asyncio.run(_load())
            if loaded:
                self._camera_gate.update(loaded)
                log.info("camera_gate_map_loaded", n=len(loaded))
        except Exception as exc:  # noqa: BLE001
            log.warning("camera_gate_map_fallback", error=str(exc))

    # -- ingest from kafka --------------------------------------------------
    def _on_anpr(self, value: dict) -> None:
        ANPR_SEEN.inc()
        camera_id = value.get("camera_id")
        plate = value.get("plate")
        if not camera_id or not plate or plate in ("NO_FEED", "DRYRUN-CROP"):
            return
        gate = self._camera_gate.get(camera_id)
        if not gate:
            return  # corridor camera — no gate to join on
        buf = _Buffered(value=plate, extra=camera_id, mono=time.monotonic(),
                        ts=self._parse_ts(value.get("ts")))
        with self._lock:
            self._plates[gate].append(buf)
            self._match(gate)

    def _on_rfid(self, value: dict) -> None:
        RFID_SEEN.inc()
        reader_id = value.get("reader_id")
        tag = value.get("tag_id")
        if not reader_id or not tag:
            return
        gate = self._reader_gate.get(reader_id)
        if not gate:
            return  # corridor reader — not part of the per-gate join
        buf = _Buffered(value=tag, extra=reader_id, mono=time.monotonic(),
                        ts=self._parse_ts(value.get("ts")))
        with self._lock:
            self._tags[gate].append(buf)
            self._match(gate)

    @staticmethod
    def _parse_ts(raw) -> datetime:
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                pass
        return _utcnow()

    # -- the join -----------------------------------------------------------
    def _expire(self, dq: Deque[_Buffered], now: float) -> None:
        window = self.cfg.correlation_window_s
        while dq and now - dq[0].mono > window:
            dq.popleft()

    def _match(self, gate: str) -> None:
        """Greedily pair closest-in-time plate<->tag reads at a gate (1:1).

        Rather than the full cross-product (which would confirm one plate
        against every co-present tag at a busy gate), we pair each plate with
        the single nearest-in-time tag within the window and consume both, so a
        given read confirms at most once. Already-emitted (gate, plate, tag)
        triples are suppressed for one window to ride out duplicate reads.
        """
        now = time.monotonic()
        plates = self._plates[gate]
        tags = self._tags[gate]
        self._expire(plates, now)
        self._expire(tags, now)
        if not plates or not tags:
            return

        window = self.cfg.correlation_window_s
        used_plate: set[int] = set()
        used_tag: set[int] = set()
        plate_list = list(plates)
        tag_list = list(tags)

        # Build candidate pairs within the time window, nearest-in-time first.
        candidates = []
        for pi, p in enumerate(plate_list):
            for ti, t in enumerate(tag_list):
                dt = abs((p.ts - t.ts).total_seconds())
                if dt <= window:
                    candidates.append((dt, pi, ti, p, t))
        candidates.sort(key=lambda c: c[0])

        emitted_now = []
        for _dt, pi, ti, p, t in candidates:
            if pi in used_plate or ti in used_tag:
                continue
            key = (gate, p.value, t.value)
            last = self._emitted.get(key)
            if last is not None and now - last < window:
                used_plate.add(pi)
                used_tag.add(ti)
                continue
            used_plate.add(pi)
            used_tag.add(ti)
            self._emitted[key] = now
            emitted_now.append((p, t))

        for p, t in emitted_now:
            self._emit_confirmed(gate, p, t)

    def _emit_confirmed(self, gate: str, plate: _Buffered, tag: _Buffered) -> None:
        event = {
            "ts": _utcnow().isoformat(),
            "plate": plate.value,
            "rfid_tag": tag.value,
            "camera_id": plate.extra,
            "gate_id": gate,
            "confidence": self.cfg.correlator_confidence,
        }
        try:
            kafka_io.produce(self._producer, self.cfg.confirmed_topic, event,
                             key=gate, flush=False,
                             event_type="jnpa.vehicle.confirmed",
                             source_system="SIM",
                             raw_ref=f"correlate://{gate}#plate={plate.value}&tag={tag.value}")
            self._producer.poll(0)
            VEHICLE_CONFIRMED.labels(gate_id=gate).inc()
            log.info("vehicle.confirmed", **event)
        except Exception as exc:  # noqa: BLE001
            KAFKA_ERRORS.inc()
            log.warning("confirmed_emit_failed", error=str(exc), gate_id=gate)

    def _gc_emitted(self) -> None:
        now = time.monotonic()
        with self._lock:
            stale = [k for k, v in self._emitted.items()
                     if now - v > self.cfg.correlation_window_s * 4]
            for k in stale:
                del self._emitted[k]

    # -- consumer threads ---------------------------------------------------
    def _consume_loop(self, topic: str, group: str, handler) -> None:
        """Run a resilient confluent-kafka consume loop until stop is set.

        Uses ``auto.offset.reset=latest``: a real-time correlator joins a 5 s
        window, so on (re)start it should pick up from the live edge rather than
        replay the whole backlog and emit thousands of stale confirmations.
        """
        from confluent_kafka import Consumer

        while not self._stop.is_set():
            consumer = None
            try:
                consumer = Consumer({
                    "bootstrap.servers": self.cfg.kafka_brokers,
                    "group.id": group,
                    "auto.offset.reset": "latest",
                    "enable.auto.commit": True,
                    "session.timeout.ms": 10000,
                })
                consumer.subscribe([topic])
                while not self._stop.is_set():
                    msg = consumer.poll(0.5)
                    if msg is None:
                        continue
                    if msg.error():
                        raise RuntimeError(str(msg.error()))
                    from jnpa_shared import cloudevents
                    handler(cloudevents.unwrap(kafka_io.decode_value(msg.value())))
            except Exception as exc:  # noqa: BLE001
                log.warning("consume_loop_error", topic=topic, error=str(exc))
                self._stop.wait(2.0)
            finally:
                if consumer is not None:
                    try:
                        consumer.close()
                    except Exception:  # noqa: BLE001
                        pass

    def run(self) -> None:
        self._load_camera_gate_map()
        log.info(
            "correlator_started",
            window_s=self.cfg.correlation_window_s,
            gate_readers=len(self._reader_gate),
            cameras=len(self._camera_gate),
        )
        threads = [
            threading.Thread(
                target=self._consume_loop,
                args=(self.cfg.anpr_topic, f"{self.cfg.correlator_group}-anpr", self._on_anpr),
                name="anpr-consumer", daemon=True,
            ),
            threading.Thread(
                target=self._consume_loop,
                args=(self.cfg.rfid_topic, f"{self.cfg.correlator_group}-rfid", self._on_rfid),
                name="rfid-consumer", daemon=True,
            ),
        ]
        for t in threads:
            t.start()

        last_stats = time.monotonic()
        while not self._stop.is_set():
            self._stop.wait(2.0)
            self._gc_emitted()
            if time.monotonic() - last_stats >= 5.0:
                log.info(
                    "correlator_stats",
                    anpr_seen=int(counter_total(ANPR_SEEN)),
                    rfid_seen=int(counter_total(RFID_SEEN)),
                    confirmed=int(counter_total(VEHICLE_CONFIRMED)),
                )
                last_stats = time.monotonic()

        for t in threads:
            t.join(timeout=3.0)
        try:
            self._producer.flush(5)
        except Exception:  # noqa: BLE001
            pass
        log.info("correlator_stopped")

    def request_stop(self) -> None:
        self._stop.set()


def run() -> None:
    cfg = RfidConfig.from_env()
    configure_logging(cfg.log_level)
    start_metrics_server(cfg.metrics_port)
    corr = Correlator(cfg)

    def _request_stop(*_a) -> None:
        log.info("shutdown_signal_received")
        corr.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass

    try:
        corr.run()
    except KeyboardInterrupt:
        corr.request_stop()


if __name__ == "__main__":
    run()
