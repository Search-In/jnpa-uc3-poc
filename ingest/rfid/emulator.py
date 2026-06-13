"""RFID reader emulator — JNPA UC-III PoC.

Starts 25 logical readers (10 at the 4 gates, 15 along the 40-km NH-348
corridor). Each reader owns an independent Poisson process of vehicle
pass-throughs; the mean rate is higher during the IST peak windows
(08:00-11:00 and 18:00-21:00). Every pass-through publishes one JSON read:

    topic:   rfid/readers/{reader_id}
    payload: {"ts": ISO8601, "reader_id": "R-08",
              "tag_id": "E2801160...", "rssi": -42.3}

Tag ids are drawn from a fixed pool of 12,000 so the *same* truck shows up at
multiple readers as it moves. We model this with lightweight "truck journeys":
a truck enters at a gate, then is seen at successive corridor readers as it
travels — so a given tag genuinely appears across readers within a few seconds,
which is what the correlator (and the rfid<->anpr join) needs.

Resilient to broker restart: the MQTT client auto-reconnects with backoff, and
publishes made while disconnected are queued/dropped without crashing the loop.

Run with ``rfid-emulator`` (console script) or ``python -m rfid_ingest.emulator``.
"""
from __future__ import annotations

import random
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Deque, List, Optional

from jnpa_shared import kafka_io  # noqa: F401  (keeps shared import surface warm)
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import RfidRead

from rfid_ingest import mqtt_io
from rfid_ingest.config import MQTT_TOPIC_PREFIX, RfidConfig
from rfid_ingest.metrics import (
    ACTIVE_READERS,
    MQTT_PUBLISH_ERRORS,
    RFID_PUBLISHED,
    start_metrics_server,
)
from rfid_ingest.topology import Reader, build_readers, build_tag_pool

log = get_logger("rfid_ingest.emulator")


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def _is_peak(cfg: RfidConfig, now: Optional[datetime] = None) -> bool:
    """True if the current wall-clock IST hour falls in a configured peak window."""
    now = now or _utcnow()
    ist_hour = (now.hour + now.minute / 60.0 + cfg.ist_offset_hours) % 24.0
    for lo, hi in cfg.peak_windows_ist:
        if lo <= ist_hour < hi:
            return True
    return False


@dataclass
class _Truck:
    """A truck currently traversing the corridor, identified by its tag."""

    tag_id: str
    next_reader_idx: int   # index into the ordered reader fleet
    next_seen_at: float    # monotonic time the next reader should see it


class Emulator:
    """Drives the reader fleet and publishes reads over MQTT."""

    def __init__(self, cfg: RfidConfig) -> None:
        self.cfg = cfg
        self.readers: List[Reader] = build_readers(
            cfg.num_gate_readers, cfg.num_corridor_readers
        )
        self.tag_pool: List[str] = build_tag_pool(cfg.tag_pool_size, seed=cfg.seed)
        self.rng = random.Random(cfg.seed)
        # In-flight trucks (each will be re-seen at successive readers).
        self._trucks: Deque[_Truck] = deque()
        self._stop = threading.Event()
        self.client = mqtt_io.build_client(cfg, client_id="rfid-emulator")

    # -- publish ------------------------------------------------------------
    def _publish(self, reader: Reader, tag_id: str) -> None:
        rssi = round(self.rng.gauss(self.cfg.rssi_mean, self.cfg.rssi_jitter / 3.0), 1)
        read = RfidRead(ts=_utcnow(), reader_id=reader.id, tag_id=tag_id, rssi=rssi)
        topic = f"{MQTT_TOPIC_PREFIX}/{reader.id}"
        payload = read.model_dump_json()
        info = self.client.publish(topic, payload, qos=self.cfg.mqtt_qos)
        if info.rc != 0:
            # Disconnected: paho queues QoS>0, drops QoS0. Count but never crash.
            MQTT_PUBLISH_ERRORS.inc()
            log.debug("mqtt_publish_deferred", reader_id=reader.id, rc=info.rc)
            return
        RFID_PUBLISHED.labels(reader_id=reader.id).inc()

    # -- movement model -----------------------------------------------------
    def _spawn_truck(self) -> None:
        """A new truck enters at a gate reader and will move down the corridor."""
        tag = self.rng.choice(self.tag_pool)
        # Gate readers are first in the fleet; enter at one of them.
        gate_idx = self.rng.randrange(self.cfg.num_gate_readers)
        self._trucks.append(
            _Truck(tag_id=tag, next_reader_idx=gate_idx, next_seen_at=time.monotonic())
        )

    def _advance_trucks(self, now: float) -> None:
        """Re-publish in-flight trucks at the next reader once they 'arrive'."""
        # Process trucks whose next reader is due.
        pending = len(self._trucks)
        for _ in range(pending):
            truck = self._trucks.popleft()
            if now < truck.next_seen_at:
                self._trucks.append(truck)
                continue
            reader = self.readers[truck.next_reader_idx]
            self._publish(reader, truck.tag_id)
            # Move to the next corridor reader, if any, after a short travel time.
            nxt = truck.next_reader_idx + 1
            if nxt < len(self.readers):
                # ~2-6 s between readers (corridor segments are ~1.8 km).
                travel = self.rng.uniform(2.0, 6.0)
                truck.next_reader_idx = nxt
                truck.next_seen_at = now + travel
                self._trucks.append(truck)
            # else: truck has left the monitored corridor; drop it.

    # -- main loop ----------------------------------------------------------
    def run(self) -> None:
        mqtt_io.start(self.cfg, self.client)
        ACTIVE_READERS.set(len(self.readers))
        log.info(
            "emulator_started",
            readers=len(self.readers),
            gate_readers=self.cfg.num_gate_readers,
            corridor_readers=self.cfg.num_corridor_readers,
            tag_pool=len(self.tag_pool),
        )

        tick = 0.25  # seconds; Poisson sampling granularity
        last_stats = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            peak = _is_peak(self.cfg)
            mult = self.cfg.peak_rate_multiplier if peak else 1.0

            # Each reader is an independent Poisson source of NEW truck arrivals.
            # Per tick, expected new arrivals = rate * num_readers * tick.
            lam = self.cfg.base_rate_per_reader * mult * len(self.readers) * tick
            n_new = self._poisson(lam)
            for _ in range(n_new):
                self._spawn_truck()
                # The spawning gate reader sees the truck immediately.
            self._advance_trucks(now)

            if now - last_stats >= 5.0:
                log.info(
                    "emulator_stats",
                    published=int(_counter_total(RFID_PUBLISHED)),
                    in_flight=len(self._trucks),
                    peak=peak,
                )
                last_stats = now

            self._stop.wait(tick)

        mqtt_io.stop(self.client)
        log.info("emulator_stopped", published=int(_counter_total(RFID_PUBLISHED)))

    def _poisson(self, lam: float) -> int:
        """Knuth's Poisson sampler (lam is small here, so this is cheap)."""
        if lam <= 0:
            return 0
        import math

        l = math.exp(-lam)
        k = 0
        p = 1.0
        while True:
            k += 1
            p *= self.rng.random()
            if p <= l:
                return k - 1

    def request_stop(self) -> None:
        self._stop.set()


def _counter_total(counter) -> float:
    from rfid_ingest.metrics import counter_total

    return counter_total(counter)


def run() -> None:
    """Console-script / module entrypoint."""
    cfg = RfidConfig.from_env()
    configure_logging(cfg.log_level)
    start_metrics_server(cfg.metrics_port)
    emu = Emulator(cfg)

    def _request_stop(*_a) -> None:
        log.info("shutdown_signal_received")
        emu.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            pass

    try:
        emu.run()
    except KeyboardInterrupt:
        emu.request_stop()


if __name__ == "__main__":
    run()
