"""Periodic backbone publisher for the HTTP-only capability services.

The parking / carbon / gate-data / identity / empty-container services answer
over HTTP and were never on the event backbone. To bring them to the simulator
standard — *the dashboard can't tell SIM from LIVE except via the mode badge* —
each one now also publishes its current state onto a Kafka topic on a timer,
wrapped in a CloudEvents 1.0 envelope tagged ``sourcesystem=SIM``.

This module gives every service that capability in ~3 lines from its FastAPI
lifespan::

    from jnpa_shared.backbone import PeriodicPublisher
    pub = PeriodicPublisher("parking", TOPIC_PARKING, "jnpa.parking.state", snapshot_fn)
    async with _lifespan ...:
        pub.start(); yield; await pub.stop()

Design notes
------------
* **Never breaks the HTTP API.** Kafka unavailability (broker down, offline run)
  is logged and the loop keeps ticking; the service stays up.
* **Offline-aware.** When ``settings.is_offline`` is true we still publish to the
  *internal* broker (it's part of the offline stack) but never touch any
  external network — the snapshot_fn is the only data source.
* **Deterministic.** The snapshot_fn is expected to be a pure function of state
  (the capability services already compute deterministic snapshots), so a given
  tick yields the same event under the same seed/clock.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Iterable, Optional

from .config import get_settings
from .logging import get_logger

log = get_logger("backbone")

# A snapshot fn returns either one event payload or an iterable of them. Each is
# a pydantic model or a JSON-able dict.
SnapshotFn = Callable[[], Any]


class PeriodicPublisher:
    """Publishes a service's snapshot onto a Kafka topic on a fixed interval."""

    def __init__(
        self,
        component: str,
        topic: str,
        event_type: str,
        snapshot_fn: SnapshotFn,
        *,
        interval_s: float = 5.0,
        key_fn: Optional[Callable[[Any], Optional[str]]] = None,
        raw_ref_fn: Optional[Callable[[Any], Optional[str]]] = None,
        source_system: str = "SIM",
    ) -> None:
        self.component = component
        self.topic = topic
        self.event_type = event_type
        self.snapshot_fn = snapshot_fn
        self.interval_s = interval_s
        self.key_fn = key_fn
        self.raw_ref_fn = raw_ref_fn
        self.source_system = source_system
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._producer = None
        self.published = 0  # exposed for the self-test

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name=f"backbone:{self.component}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        if self._producer is not None:
            try:
                self._producer.flush(2.0)
            except Exception:  # noqa: BLE001
                pass

    # -- internals ----------------------------------------------------------
    def _ensure_producer(self):
        if self._producer is None:
            from . import kafka_io

            self._producer = kafka_io.get_producer(
                {"client.id": f"{self.component}-backbone"}
            )
        return self._producer

    def publish_once(self) -> int:
        """Publish the current snapshot once. Returns events published.

        Synchronous and best-effort: any Kafka error is swallowed (logged) so
        callers — including the timer loop and the self-test — never crash.
        """
        try:
            from . import kafka_io

            snap = self.snapshot_fn()
            events: Iterable[Any]
            if snap is None:
                return 0
            events = snap if isinstance(snap, (list, tuple)) else [snap]
            producer = self._ensure_producer()
            n = 0
            for ev in events:
                key = self.key_fn(ev) if self.key_fn else None
                raw_ref = self.raw_ref_fn(ev) if self.raw_ref_fn else None
                kafka_io.produce(
                    producer, self.topic, ev, key=key, flush=False,
                    event_type=self.event_type,
                    source_system=self.source_system,
                    raw_ref=raw_ref,
                )
                n += 1
            producer.flush(2.0)
            self.published += n
            return n
        except Exception as exc:  # noqa: BLE001 — never break the HTTP API
            log.warning("backbone_publish_failed", component=self.component, error=str(exc))
            return 0

    async def _run(self) -> None:
        settings = get_settings()
        log.info("backbone_publisher_started", component=self.component,
                 topic=self.topic, offline=settings.is_offline,
                 interval_s=self.interval_s)
        while not self._stop.is_set():
            await asyncio.to_thread(self.publish_once)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass


__all__ = ["PeriodicPublisher", "SnapshotFn"]
