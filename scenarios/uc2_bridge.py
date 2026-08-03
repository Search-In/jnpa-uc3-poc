"""UC-II <-> UC-III cross-twin bridge.

UC-II (the cargo/DPD twin) publishes a release-spike event to the Kafka topic
``cargo.dpd_release`` when DPD (Direct Port Delivery) volumes surge. UC-III
treats that as a leading indicator of upstream truck demand.

``translate_release`` turns a release-spike multiplier into an expected upstream
truck demand profile: a baseline of ~240 trucks/h scaled by the multiplier,
released as bursts over a window (default 40 min).

The bridge runs a REAL Kafka consumer (group ``uc3-uc2-bridge``, started from
the scenarios-runner lifespan):

  * events carrying a ``correlation_id`` belong to a running TFC-3 — they are
    queued and TFC-3 awaits its own event back through the broker
    (``next_event``), proving publish -> broker -> consume end-to-end. If the
    broker is down TFC-3 falls back to its inline copy and records the step
    ``degraded`` — the demo never stalls, the timeline stays honest.
  * events WITHOUT a ``correlation_id`` are treated as a genuine external UC-II
    push: the bridge autonomously translates the profile and instantiates
    trucks on the corridor (tag ``UC2-BRIDGE:<n>``), so a real UC-II stack can
    drive UC-III without any scenario running.
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from jnpa_shared import kafka_io
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import TOPIC_DPD_RELEASE, DpdReleaseEvent

log = get_logger("scenarios.uc2_bridge")

CONSUMER_GROUP = "uc3-uc2-bridge"
# Cap for autonomously handled external events (mirrors tfc3.MAX_INJECT).
EXTERNAL_MAX_INJECT = 300

# Cross-twin Kafka topic + typed event live in the shared schemas package (XT-1):
# defined ONCE so UC-II (producer) and UC-III (consumer) agree on the contract.
# Re-exported here for the existing call sites.

# Baseline corridor truck demand (trucks/hour) at 1.0x release.
BASELINE_TRUCKS_PER_H = 240


@dataclass
class DemandProfile:
    """Translated UC-III truck demand from a UC-II DPD release spike."""

    multiplier: float
    trucks_per_h: int
    window_min: int
    total_trucks: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "multiplier": self.multiplier,
            "trucks_per_h": self.trucks_per_h,
            "window_min": self.window_min,
            "total_trucks": self.total_trucks,
        }


def translate_release(event: "Dict[str, Any] | DpdReleaseEvent") -> DemandProfile:
    """Translate a ``cargo.dpd_release`` event into a UC-III demand profile.

    Accepts either the typed cross-twin model (``DpdReleaseEvent``) or the raw
    dict UC-II would put on the wire:
        {"dpd_release_spike": 2.5, "window_min": 40}
    The spec's TFC-3 calls for "bursts of 600 trucks/h released over 40 min";
    that is exactly 2.5x the 240/h baseline, so the defaults reproduce it.
    """
    if isinstance(event, DpdReleaseEvent):
        event = event.model_dump()
    mult = float(event.get("dpd_release_spike", 1.0))
    window_min = int(event.get("window_min", 40))
    trucks_per_h = int(round(BASELINE_TRUCKS_PER_H * mult))
    total = int(round(trucks_per_h * (window_min / 60.0)))
    profile = DemandProfile(
        multiplier=mult, trucks_per_h=trucks_per_h, window_min=window_min, total_trucks=total,
    )
    log.info("dpd_release_translated", **profile.to_dict())
    return profile


# --------------------------------------------------------------------- listener
# One consumer thread per scenarios-runner process, started from its lifespan.
_STOP = threading.Event()
_ASSIGNED = threading.Event()
_LOOP: Optional[asyncio.AbstractEventLoop] = None
_QUEUE: Optional["asyncio.Queue[Dict[str, Any]]"] = None
_TASK: Optional[asyncio.Future] = None


def is_listening() -> bool:
    return _TASK is not None and not _TASK.done()


def wait_assigned(timeout: float = 15.0) -> bool:
    """Block until the consumer has partition assignments (or timeout).

    With ``auto.offset.reset=latest`` a message produced before assignment is
    silently skipped — callers that publish-then-await (TFC-3, tests) should
    wait for this first. Returns False on timeout / listener down.
    """
    return _ASSIGNED.wait(timeout)


def _enqueue(evt: Dict[str, Any]) -> None:
    """Runs on the event loop. Drop-oldest so a burst can never block the pump."""
    assert _QUEUE is not None
    if _QUEUE.full():
        try:
            _QUEUE.get_nowait()
        except asyncio.QueueEmpty:  # pragma: no cover - racy by definition
            pass
    _QUEUE.put_nowait(evt)


async def _handle_external(evt: Dict[str, Any]) -> None:
    """A genuine UC-II push (no correlation_id): translate + instantiate trucks.

    This is what makes the cross-twin link real without a scenario running —
    an external producer on ``cargo.dpd_release`` moves the road twin.
    Injected trucks are tagged ``UC2-BRIDGE:<ts>`` so they are identifiable
    (and removable via truck-sim's tagged-delete) like scenario trucks.
    """
    from .base import Upstreams
    from .config import ScenarioConfig

    profile = translate_release(evt)
    count = min(profile.total_trucks, EXTERNAL_MAX_INJECT)
    tag = f"UC2-BRIDGE:{int(time.time())}"
    up = Upstreams(ScenarioConfig.from_env())
    try:
        inj = await up.truck_post("/devices/inject", {
            "count": count, "tag": tag, "state": "EN_ROUTE_TO_PORT",
        })
        log.info("uc2_external_release_applied", tag=tag,
                 requested=count, injected=(inj or {}).get("injected"),
                 profile=profile.to_dict())
    finally:
        await up.aclose()


def _dispatch(evt: Any) -> None:
    """Runs on the consumer thread: route one decoded event."""
    if _LOOP is None:
        return
    if not isinstance(evt, dict):
        log.warning("dpd_release_ignored_non_dict", type=type(evt).__name__)
        return
    if evt.get("correlation_id"):
        # A running TFC-3 owns this event — queue it for next_event().
        _LOOP.call_soon_threadsafe(_enqueue, evt)
    else:
        asyncio.run_coroutine_threadsafe(_handle_external(evt), _LOOP)


def _pump(group: str) -> None:
    """Blocking consumer loop (worker thread). ``kafka_io.consume`` only checks
    ``stop_when`` after a message, so we run our own poll loop for clean idle
    shutdown."""
    from jnpa_shared import cloudevents

    def _on_assign(consumer_, partitions) -> None:
        # Pin the fetch position to the high-watermark AT ASSIGNMENT so any
        # message produced after this callback is guaranteed delivered.
        # (`auto.offset.reset=latest` alone resolves the position lazily at
        # first fetch — a message produced in that window is silently skipped,
        # which is exactly the publish-then-await pattern TFC-3 uses.)
        for tp in partitions:
            try:
                _lo, hi = consumer_.get_watermark_offsets(tp, timeout=5.0)
                tp.offset = hi
            except Exception:  # noqa: BLE001 - fall back to lazy 'latest'
                pass
        consumer_.assign(partitions)
        log.info("uc2_bridge_assigned", partitions=len(partitions))
        _ASSIGNED.set()

    try:
        consumer = kafka_io.get_consumer(group, {"auto.offset.reset": "latest"})
        consumer.subscribe([TOPIC_DPD_RELEASE], on_assign=_on_assign)
    except Exception as exc:  # noqa: BLE001 - broker down => listener unavailable
        log.warning("uc2_bridge_consumer_unavailable", error=str(exc))
        return
    log.info("uc2_bridge_listening", topic=TOPIC_DPD_RELEASE, group=group)
    try:
        while not _STOP.is_set():
            msg = consumer.poll(1.0)
            if msg is None:
                continue
            if msg.error():
                log.warning("uc2_bridge_consume_error", error=str(msg.error()))
                continue
            try:
                _dispatch(cloudevents.unwrap(kafka_io.decode_value(msg.value())))
            except Exception as exc:  # noqa: BLE001 - one bad message never kills the loop
                log.warning("uc2_bridge_dispatch_failed", error=str(exc))
    finally:
        consumer.close()
        log.info("uc2_bridge_stopped")


async def start_listener(group: str = CONSUMER_GROUP) -> None:
    """Start the bridge consumer (idempotent; called from the runner lifespan)."""
    global _LOOP, _QUEUE, _TASK
    if is_listening():
        return
    _LOOP = asyncio.get_running_loop()
    _QUEUE = asyncio.Queue(maxsize=64)
    _STOP.clear()
    _ASSIGNED.clear()
    _TASK = _LOOP.run_in_executor(None, _pump, group)


async def stop_listener() -> None:
    global _TASK
    _STOP.set()
    if _TASK is not None:
        try:
            await asyncio.wait_for(_TASK, timeout=5.0)
        except Exception:  # noqa: BLE001 - shutdown is best-effort
            pass
        _TASK = None


async def next_event(timeout: float = 10.0) -> Dict[str, Any]:
    """Await the next correlation-tagged event consumed from the broker.

    Raises ``asyncio.TimeoutError`` when the listener is down or nothing
    arrives — the caller (TFC-3) then falls back inline and reports the step
    ``degraded``.
    """
    if _QUEUE is None or not is_listening():
        raise asyncio.TimeoutError("uc2_bridge listener not running")
    return await asyncio.wait_for(_QUEUE.get(), timeout)


__all__ = [
    "TOPIC_DPD_RELEASE",
    "DpdReleaseEvent",
    "BASELINE_TRUCKS_PER_H",
    "CONSUMER_GROUP",
    "DemandProfile",
    "translate_release",
    "start_listener",
    "stop_listener",
    "next_event",
    "is_listening",
]
