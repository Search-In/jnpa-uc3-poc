"""The simulation engine: advances every truck on its cadence and fans out
telemetry + ETA to the three sinks.

Design for 20k–30k devices on a 4-core laptop (4,000 msg/s at the 5 s interval):

  * We do NOT spawn one asyncio task per truck (20k tasks thrash the loop).
    Instead a single **tick scheduler** runs at a fixed cadence (``tick_s``,
    default 1 s). Each truck carries its own ``next_update`` deadline; on every
    tick we process exactly the trucks whose deadline has passed, advance them by
    the real elapsed time, and re-arm their deadline (5 s normally, 2 s when
    AT_GATE_QUEUE). The deadlines are spread across the interval at populate time
    so the per-tick batch is ~population/interval — e.g. 20k/5 s ≈ 4k trucks/s,
    smoothed to ~4k/tick — never a thundering herd.

  * Publishing is synchronous-cheap: MQTT publish is awaited (aiomqtt), Kafka is
    fire-and-forget, the DB row is buffered for the periodic COPY. A bounded set
    of route-binding coroutines runs concurrently so OSRM latency never stalls
    the tick.

  * Two background tasks handle the periodic work: the **ETA loop** (every 30 s,
    recompute + publish ETA per truck, throttled in slices) and the **DB flush
    loop** (every 30 s, COPY the telemetry buffer to Timescale).
"""
from __future__ import annotations

import asyncio
import time
from typing import List

from jnpa_shared.logging import get_logger

from .config import TruckConfig
from .fleet import Fleet
from .metrics import (
    PUBLISH_RATE,
    STATE_TRANSITIONS,
    TRUCKS_BY_STATE,
    TRUCKS_TOTAL,
)
from .sinks import DbSink, KafkaSink, MqttSink
from .truck import Truck, TruckState

log = get_logger("trucking_app.simulator")


class Simulator:
    """Drives the fleet and fans telemetry out to MQTT + Kafka + Timescale."""

    def __init__(self, cfg: TruckConfig, fleet: Fleet) -> None:
        self.cfg = cfg
        self.fleet = fleet
        self.mqtt = MqttSink(cfg)
        self.kafka = KafkaSink(cfg)
        self.db = DbSink(cfg)
        self._stop = asyncio.Event()
        self._tick_s = 1.0
        # Concurrency guard for in-flight route fetches kicked off by the tick.
        self._route_inflight: set[str] = set()
        self._published_window = 0
        self._tasks: List[asyncio.Task] = []
        # Fire-and-forget publish / route-bind tasks (kept referenced so the
        # event loop doesn't garbage-collect them before they complete).
        self._bg_tasks: set[asyncio.Task] = set()

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        await self.db.start()
        await self.mqtt.start()
        # Arm initial deadlines spread across the update interval.
        self._arm_deadlines()
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name="tick"),
            asyncio.create_task(self._eta_loop(), name="eta"),
            asyncio.create_task(self._db_flush_loop(), name="db-flush"),
            asyncio.create_task(self._kafka_poll_loop(), name="kafka-poll"),
            asyncio.create_task(self._stats_loop(), name="stats"),
        ]
        log.info("simulator_started", devices=len(self.fleet.trucks))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Drain any in-flight publish / route-bind tasks.
        for t in list(self._bg_tasks):
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self.kafka.flush()
        await self.db.close()
        await self.mqtt.close()
        log.info("simulator_stopped")

    def _arm_deadlines(self) -> None:
        """Spread each truck's first update across [0, interval) so the per-tick
        batch is even from the very first tick."""
        now = time.monotonic()
        n = max(1, len(self.fleet.trucks))
        interval = self.cfg.interval_default_s
        for i, truck in enumerate(self.fleet.trucks.values()):
            truck_next = now + (i % n) / n * interval
            setattr(truck, "_next_update", truck_next)

    # -- tick scheduler -----------------------------------------------------
    async def _tick_loop(self) -> None:
        """Every tick, process the trucks whose update deadline has passed.

        Each due truck is advanced synchronously (cheap: motion + Kafka enqueue +
        DB buffer), then all the tick's MQTT publishes are issued together with a
        single ``gather`` so the network fan-out doesn't serialize the loop. This
        keeps the per-tick cost ~O(due) and lets 20k devices stay on schedule.
        """
        # Track per-truck last-processed time for accurate dt.
        last_seen: dict[str, float] = {}
        while not self._stop.is_set():
            tick_start = time.monotonic()
            due: List[Truck] = [
                t for t in self.fleet.trucks.values()
                if getattr(t, "_next_update", 0.0) <= tick_start
            ]

            # Warm the jam-factor cache for every segment this batch touches, so
            # the per-truck advance below reads it without awaiting Redis.
            await self._prefetch_jam(due)

            publishes = []
            for truck in due:
                dev = truck.profile.device_id
                last = last_seen.get(dev, tick_start - self.cfg.interval_default_s)
                dt = max(0.1, tick_start - last)
                last_seen[dev] = tick_start
                publishes.append(self._advance_and_event(truck, dt))
                interval = (
                    self.cfg.interval_at_gate_s
                    if truck.state == TruckState.AT_GATE_QUEUE
                    else self.cfg.interval_default_s
                )
                setattr(truck, "_next_update", tick_start + interval)

            if publishes:
                await asyncio.gather(*publishes, return_exceptions=True)

            # Drop bookkeeping for trucks that were scaled away.
            if len(last_seen) > len(self.fleet.trucks) * 2:
                live = set(self.fleet.trucks.keys())
                last_seen = {k: v for k, v in last_seen.items() if k in live}

            elapsed = time.monotonic() - tick_start
            await asyncio.sleep(max(0.0, self._tick_s - elapsed))

    async def _prefetch_jam(self, trucks: List[Truck]) -> None:
        """Populate the fleet's jam cache for every segment in this batch."""
        segs = {t.current_segment_id for t in trucks}
        segs.discard(None)
        for seg in segs:
            await self.fleet.jam_factor(seg)  # cached afterwards (5 s TTL)

    def _advance_and_event(self, truck: Truck, dt: float):
        """Advance one truck synchronously and return its MQTT-publish coroutine.

        Route binding is kicked off as a *background* task (OSRM latency must not
        stall the tick). A truck still awaiting its first route stays parked at
        its origin and keeps emitting telemetry — so every device publishes on
        schedule, route or not. Kafka + DB writes happen here (cheap, non-async);
        only the MQTT publish is returned for the caller to ``gather``.
        """
        if truck.needs_route:
            self._kick_route(truck)
        else:
            jam = self.fleet.jam_factor_cached(truck.current_segment_id)
            prev_state = truck.state
            new_state = truck.advance(dt, jam)
            if new_state is not None:
                STATE_TRANSITIONS.labels(new_state.value).inc()
                TRUCKS_BY_STATE.labels(prev_state.value).dec()
                TRUCKS_BY_STATE.labels(new_state.value).inc()
                # A leg just finished -> the next driving leg needs a route; kick
                # it now so the polyline is ready before the next tick.
                if truck.needs_route:
                    self._kick_route(truck)

        event = truck.telemetry()
        self.kafka.publish_telemetry(truck.profile.device_id, event)
        self.db.enqueue(event)
        self._published_window += 1
        return self.mqtt.publish_telemetry(truck.profile.device_id, event.model_dump_json())

    def _kick_route(self, truck: Truck) -> None:
        """Spawn a bounded background route fetch for ``truck`` (non-blocking)."""
        dev = truck.profile.device_id
        if dev in self._route_inflight:
            return
        if len(self._route_inflight) >= self.cfg.routing_max_concurrency:
            return  # backpressure: a later tick will retry
        self._route_inflight.add(dev)

        async def _bind() -> None:
            try:
                await self.fleet.ensure_route(truck)
            except Exception as exc:  # noqa: BLE001
                log.debug("route_bind_failed", device_id=dev, error=str(exc))
            finally:
                self._route_inflight.discard(dev)

        self._spawn(_bind())

    def _spawn(self, coro) -> None:
        """Track a fire-and-forget task so it isn't GC'd mid-flight."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # -- ETA loop -----------------------------------------------------------
    async def _eta_loop(self) -> None:
        """Every eta_interval_s, recompute + publish ETA-to-gate for each truck.

        Sliced across the interval so 20k OSRM/duration calls don't burst — we
        process ~population/interval trucks per second."""
        while not self._stop.is_set():
            cycle_start = time.monotonic()
            devices = list(self.fleet.trucks.keys())
            n = len(devices)
            if n == 0:
                await self._sleep(self.cfg.eta_interval_s)
                continue
            # Spread the n ETA computations across the interval in ~1 s slices.
            slices = max(1, int(self.cfg.eta_interval_s))
            per_slice = max(1, (n + slices - 1) // slices)
            for start in range(0, n, per_slice):
                if self._stop.is_set():
                    break
                batch = devices[start:start + per_slice]
                await asyncio.gather(
                    *(self._publish_eta(dev) for dev in batch),
                    return_exceptions=True,
                )
                await asyncio.sleep(1.0)
            # Pad out the rest of the cycle if we finished early.
            spent = time.monotonic() - cycle_start
            await self._sleep(max(0.0, self.cfg.eta_interval_s - spent))

    async def _publish_eta(self, device_id: str) -> None:
        truck = self.fleet.trucks.get(device_id)
        if truck is None:
            return
        eta_s = await self.fleet.compute_eta_s(truck)
        if eta_s is None:
            return
        payload = {
            "ts": truck.telemetry().ts.isoformat(),
            "device_id": device_id,
            "plate": truck.profile.plate,
            "gate_id": truck.profile.gate_id,
            "state": truck.state.value,
            "eta_s": round(eta_s, 1),
            "eta_min": round(eta_s / 60.0, 2),
            "remaining_km": round(truck.remaining_km, 3),
        }
        import json

        body = json.dumps(payload, separators=(",", ":"))
        await self.mqtt.publish_eta(device_id, body)
        self.kafka.publish_eta(device_id, payload)

    # -- DB flush + Kafka poll ----------------------------------------------
    async def _db_flush_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(self.cfg.db_flush_interval_s)
            try:
                written = await self.db.flush()
                if written:
                    log.info("telemetry_flushed", rows=written)
            except Exception as exc:  # noqa: BLE001
                log.warning("db_flush_error", error=str(exc))

    async def _kafka_poll_loop(self) -> None:
        # Service librdkafka delivery callbacks so the producer queue drains.
        while not self._stop.is_set():
            self.kafka.poll()
            await asyncio.sleep(0.5)

    async def _stats_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep(5.0)
            rate = self._published_window / 5.0
            PUBLISH_RATE.set(rate)
            self._published_window = 0
            TRUCKS_TOTAL.set(len(self.fleet.trucks))
            log.info(
                "simulator_stats",
                devices=len(self.fleet.trucks),
                publish_rate=round(rate, 1),
                routes_inflight=len(self._route_inflight),
            )

    async def _sleep(self, seconds: float) -> None:
        """Interruptible sleep that returns immediately on stop."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
