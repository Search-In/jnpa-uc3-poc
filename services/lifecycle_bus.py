"""UC-III lifecycle event bus — Kafka + WebSocket fan-out for lifecycle milestones.

Before this, every UC-III lifecycle "event" (cargo.released, customs.rms_selected,
job/gate/yard/scan events) was a DB row that downstream systems could only
discover by polling. This module gives those milestones a real distribution path
using the EXISTING plumbing (jnpa_shared.kafka_io + the gateway WS broadcaster) —
it introduces no new infrastructure.

Design:
  * **Best-effort by construction.** Publishing never raises into a caller: a
    broker outage must not fail a container release. Every failure is logged and
    swallowed, and the DB row (the source of truth) is already written by then.
  * **Lazily connected.** The producer is created on first use and reused; when
    no broker is configured the module degrades to WS-only.
  * **Injectable.** ``set_ws_broadcaster`` lets the gateway hand in its live
    WebSocket hub without services/ importing gateway/ (dependency direction).
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable, Callable, Mapping, Optional

from jnpa_shared.logging import get_logger

log = get_logger("services.lifecycle_bus")

# The UC-III lifecycle topic. Milestones carry an `event` discriminator so one
# topic serves cargo, job, gate, yard and scan events (consumers filter).
TOPIC_UC3_LIFECYCLE = "jnpa.uc3.lifecycle"

_producer: Any = None
_producer_failed = False
_ws_broadcast: Optional[Callable[[str, Mapping[str, Any]], Awaitable[None]]] = None


def set_ws_broadcaster(fn: Optional[Callable[[str, Mapping[str, Any]], Awaitable[None]]]) -> None:
    """Register the gateway's WebSocket broadcaster (called once at startup)."""
    global _ws_broadcast
    _ws_broadcast = fn


def reset_for_tests() -> None:
    global _producer, _producer_failed, _ws_broadcast
    _producer, _producer_failed, _ws_broadcast = None, False, None


def _broker_configured() -> bool:
    """True only when a Kafka broker is explicitly configured for this process.

    Without this guard the producer is created in every context (unit tests, CLI
    importers, one-off scripts) and then spends its lifetime retrying an
    unreachable broker — which made the test suite an order of magnitude slower.
    The gateway/compose profiles always set KAFKA_BROKERS, so production is
    unaffected.

    Delegates to ``jnpa_shared.kafka_io.broker_configured`` so this rule has ONE
    definition: gateway/main.py applies the same check before starting its
    consumer pumps, and the two must not drift.
    """
    try:
        from jnpa_shared.kafka_io import broker_configured

        return broker_configured()
    except Exception:  # noqa: BLE001 — confluent_kafka may be absent; fall back
        return bool(
            (os.getenv("KAFKA_BROKERS") or "").strip()
            or (os.getenv("KAFKA_BOOTSTRAP_SERVERS") or "").strip()
        )


def _get_producer():
    """Create/reuse the Kafka producer. Returns None when unavailable (degrades
    to WS-only) and never retries after a hard failure in-process."""
    global _producer, _producer_failed
    if _producer is not None or _producer_failed:
        return _producer
    if os.getenv("UC3_BUS_ENABLED", "1") not in ("1", "true", "True") or not _broker_configured():
        _producer_failed = True
        return None
    try:
        from jnpa_shared.kafka_io import get_producer
        _producer = get_producer()
    except Exception as exc:  # noqa: BLE001 — no broker in dev/test is normal
        _producer_failed = True
        log.info("lifecycle_bus.producer_unavailable", extra={"error": str(exc)})
    return _producer


async def publish(event: str, payload: Mapping[str, Any], *,
                  key: Optional[str] = None, ws_channel: str = "uc3_lifecycle") -> dict:
    """Publish one lifecycle milestone to Kafka + the WS hub. Never raises.

    Returns ``{"kafka": bool, "ws": bool}`` so callers/tests can assert what
    actually went out without depending on infrastructure being present.
    """
    body = {"event": event, **dict(payload)}
    sent = {"kafka": False, "ws": False}

    producer = _get_producer()
    if producer is not None:
        try:
            from jnpa_shared.kafka_io import produce
            await asyncio.to_thread(
                produce, producer, TOPIC_UC3_LIFECYCLE, body,
                key or str(body.get("container_number") or body.get("job_id") or ""),
                True, event_type=event, source_system="LIVE")
            sent["kafka"] = True
        except Exception as exc:  # noqa: BLE001 — a broker blip must not fail the mutation
            log.warning("lifecycle_bus.kafka_publish_failed",
                        extra={"event": event, "error": str(exc)})

    if _ws_broadcast is not None:
        try:
            await _ws_broadcast(ws_channel, body)
            sent["ws"] = True
        except Exception as exc:  # noqa: BLE001
            log.warning("lifecycle_bus.ws_broadcast_failed",
                        extra={"event": event, "error": str(exc)})

    log.info("lifecycle_bus.published", extra={"event": event, **sent})
    return sent
