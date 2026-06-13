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
) -> None:
    """Produce one JSON message; optionally flush synchronously."""
    producer.produce(
        topic=topic,
        key=key.encode("utf-8") if key else None,
        value=encode_value(value),
    )
    if flush:
        producer.flush(10)


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
            handler(decode_value(msg.value()))
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
]
