"""confluent-kafka producer/consumer helpers.

Values are JSON-encoded UTF-8 bytes; the producer uses snappy compression.
Keep these helpers dependency-light so any service can `from jnpa_shared import
kafka_io` and publish/consume in a few lines.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Callable, Optional

from confluent_kafka import Consumer, Producer

from .config import get_settings


def _json_default(obj: Any) -> Any:
    """Serialize datetimes/dates/UUIDs/enums that the stdlib encoder rejects."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    # pydantic models
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    # enums
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


def encode_value(value: Any) -> bytes:
    """Encode a dict / pydantic model / primitive to JSON bytes."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, default=_json_default, separators=(",", ":")).encode("utf-8")


def decode_value(raw: bytes) -> Any:
    return json.loads(raw.decode("utf-8"))


def get_producer(extra_config: Optional[dict] = None) -> Producer:
    """Create a JSON+snappy producer pointed at the configured brokers."""
    settings = get_settings()
    config = {
        "bootstrap.servers": settings.kafka_brokers,
        "client.id": "jnpa-uc3-producer",
        "compression.type": "snappy",
        "enable.idempotence": True,
        "acks": "all",
        "linger.ms": 20,
    }
    if extra_config:
        config.update(extra_config)
    return Producer(config)


def produce(
    producer: Producer,
    topic: str,
    value: Any,
    key: Optional[str] = None,
    flush: bool = True,
    *,
    event_type: Optional[str] = None,
    source_system: Optional[str] = None,
    raw_ref: Optional[str] = None,
    event_id: Optional[str] = None,
    time_iso: Optional[str] = None,
) -> None:
    """Produce one JSON message; optionally flush synchronously.

    The current OpenTelemetry trace context (if any) is injected into the Kafka
    message headers as W3C ``traceparent``/``tracestate`` so a downstream
    consumer can continue the same trace (cross-service propagation). This is a
    no-op when tracing is inactive.

    CloudEvents envelope
    --------------------
    When ``event_type`` is supplied *and* ``settings.cloudevents_enabled`` is
    true, the value is wrapped in a CloudEvents 1.0 structured-mode envelope
    tagged with ``source_system`` (``SIM``/``LIVE``) and an optional
    ``raw_ref``. Binary-mode mirror headers (``ce_type``, ``ce_source_system``)
    are also set so a consumer can filter without decoding the body. Consumers
    that call :func:`consume` get the *inner* payload transparently (auto-unwrap),
    so this is fully back-compatible with pre-CloudEvents consumers.
    """
    headers = _trace_headers()
    payload: Any = value

    settings = get_settings()
    use_ce = event_type is not None and getattr(settings, "cloudevents_enabled", True)
    if use_ce:
        from . import cloudevents

        src_sys = (source_system or "SIM").upper()
        payload = cloudevents.wrap(
            value,
            event_type=event_type,
            source_system=src_sys,
            raw_ref=raw_ref,
            subject=key,
            event_id=event_id,
            time_iso=time_iso,
        )
        # Binary-mode mirror headers for header-only filtering.
        headers = list(headers) + [
            ("ce_type", event_type.encode("utf-8")),
            ("ce_source_system", src_sys.encode("utf-8")),
        ]

    producer.produce(
        topic=topic,
        key=key.encode("utf-8") if key else None,
        value=encode_value(payload),
        headers=headers or None,
    )
    if flush:
        producer.flush(10)


def _trace_headers() -> list:
    """W3C trace-context as confluent-kafka headers [(str, bytes), ...]."""
    try:
        from . import tracing

        carrier = tracing.inject_context({})
        return [(k, v.encode("utf-8")) for k, v in carrier.items()]
    except Exception:  # noqa: BLE001 - never let tracing break a produce
        return []


def headers_to_dict(msg) -> dict:
    """Decode a consumed message's Kafka headers into a {str: str} dict.

    Returns {} if the message has no headers. Consumers pass this to
    ``tracing.extract_context`` to parent the handling span on the producer's
    trace.
    """
    try:
        raw = msg.headers() or []
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for k, v in raw:
        out[k] = v.decode("utf-8") if isinstance(v, (bytes, bytearray)) else str(v)
    return out


def get_consumer(group: str, extra_config: Optional[dict] = None) -> Consumer:
    """Create a consumer in the given group, reading from the earliest offset."""
    settings = get_settings()
    config = {
        "bootstrap.servers": settings.kafka_brokers,
        "group.id": group,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": True,
        "session.timeout.ms": 10000,
    }
    if extra_config:
        config.update(extra_config)
    return Consumer(config)


def consume(
    topic: str,
    group: str,
    handler: Callable[[Any], Any],
    *,
    max_messages: Optional[int] = None,
    timeout: float = 1.0,
    poll_idle_limit: Optional[int] = None,
    stop_when: Optional[Callable[[], bool]] = None,
) -> int:
    """Consume `topic` in consumer group `group`, calling `handler(decoded_value)`
    per message.

    Returns the number of messages handled. Stops on the first of:
      * `max_messages` messages handled,
      * `stop_when()` returning True (checked after each message),
      * `poll_idle_limit` consecutive empty polls (so a bounded self-test can
        return even if its target message never arrives).
    With none of these set it loops forever (production tailing).
    """
    consumer = get_consumer(group)
    consumer.subscribe([topic])
    handled = 0
    idle = 0
    try:
        while True:
            msg = consumer.poll(timeout)
            if msg is None:
                idle += 1
                if poll_idle_limit is not None and idle >= poll_idle_limit:
                    break
                continue
            if msg.error():
                # Surface the error to the caller's logs but keep going.
                raise RuntimeError(f"kafka consume error: {msg.error()}")
            idle = 0
            from . import tracing

            with tracing.extract_context(headers_to_dict(msg), f"kafka.consume {topic}",
                                         {"messaging.system": "kafka", "messaging.destination": topic}):
                from . import cloudevents

                # Auto-unwrap a CloudEvents envelope so pre-CloudEvents handlers
                # receive the bare inner payload exactly as before.
                handler(cloudevents.unwrap(decode_value(msg.value())))
            handled += 1
            if max_messages is not None and handled >= max_messages:
                break
            if stop_when is not None and stop_when():
                break
    finally:
        consumer.close()
    return handled


__all__ = [
    "get_producer",
    "produce",
    "get_consumer",
    "consume",
    "encode_value",
    "decode_value",
    "headers_to_dict",
]
