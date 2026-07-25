"""Service configuration for the RFID emulator / consumer / correlator.

Reads from the process environment (the container gets these from compose /
.env.local) with PoC-sane defaults so every entrypoint runs out of the box.
The three services share one config object; each uses the subset it needs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jnpa_shared.config import get_settings


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


# Topic / table constants (kept here so all three services agree).
MQTT_TOPIC_PREFIX = "rfid/readers"          # publishes to rfid/readers/{reader_id}
MQTT_TOPIC_WILDCARD = "rfid/readers/+"      # consumer subscription
KAFKA_RFID_TOPIC = "rfid.reads"
KAFKA_ANPR_TOPIC = "anpr.reads"
KAFKA_CONFIRMED_TOPIC = "vehicle.confirmed"
RFID_TABLE = "core.rfid_read"


@dataclass
class RfidConfig:
    # --- Topology ---
    num_gate_readers: int = 10      # 10 readers at the 4 gates
    num_corridor_readers: int = 15  # 15 along the 40-km corridor
    tag_pool_size: int = 12000      # fixed pool so a truck is consistent across readers

    # --- Emulator pass-through rates (Poisson, reads/sec per reader) ---
    base_rate_per_reader: float = 0.15   # off-peak mean reads/sec/reader
    peak_rate_multiplier: float = 3.0    # rate during peak windows
    rssi_mean: float = -55.0
    rssi_jitter: float = 12.0

    # Peak windows in IST (hour-of-day, 24h). 08:00-11:00 and 18:00-21:00.
    peak_windows_ist: tuple[tuple[int, int], ...] = ((8, 11), (18, 21))
    ist_offset_hours: float = 5.5

    # --- MQTT ---
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_keepalive: int = 30
    mqtt_qos: int = 0
    mqtt_reconnect_min_s: float = 1.0
    mqtt_reconnect_max_s: float = 30.0

    # --- Kafka ---
    kafka_brokers: str = "kafka:9092"
    rfid_topic: str = KAFKA_RFID_TOPIC
    anpr_topic: str = KAFKA_ANPR_TOPIC
    confirmed_topic: str = KAFKA_CONFIRMED_TOPIC

    # --- Postgres / Timescale ---
    # asyncpg DSN (plain libpq form, not the SQLAlchemy "+asyncpg" form).
    postgres_dsn: str = "postgresql://postgres:jnpa_pw@postgres:5432/postgres"

    # --- Correlator ---
    correlation_window_s: float = 5.0   # join window per gate (spec)
    correlator_confidence: float = 0.97
    correlator_group: str = "rfid-correlator"
    consumer_group: str = "rfid-consumer"

    # --- Observability ---
    metrics_port: int = 9102
    log_level: str = "INFO"
    seed: int = 42

    @classmethod
    def from_env(cls) -> "RfidConfig":
        shared = get_settings()
        # Translate the shared SQLAlchemy DSN ("postgresql+asyncpg://...") into the
        # plain libpq form asyncpg.connect() expects.
        default_dsn = shared.postgres_dsn.replace("postgresql+asyncpg://", "postgresql://")
        return cls(
            num_gate_readers=_as_int(os.environ.get("RFID_GATE_READERS"), 10),
            num_corridor_readers=_as_int(os.environ.get("RFID_CORRIDOR_READERS"), 15),
            tag_pool_size=_as_int(os.environ.get("RFID_TAG_POOL_SIZE"), 12000),
            base_rate_per_reader=_as_float(os.environ.get("RFID_BASE_RATE"), 0.15),
            peak_rate_multiplier=_as_float(os.environ.get("RFID_PEAK_MULTIPLIER"), 3.0),
            rssi_mean=_as_float(os.environ.get("RFID_RSSI_MEAN"), -55.0),
            rssi_jitter=_as_float(os.environ.get("RFID_RSSI_JITTER"), 12.0),
            mqtt_host=os.environ.get("MQTT_HOST", shared.mqtt_host),
            mqtt_port=_as_int(os.environ.get("MQTT_PORT"), shared.mqtt_port),
            mqtt_keepalive=_as_int(os.environ.get("MQTT_KEEPALIVE"), 30),
            mqtt_qos=_as_int(os.environ.get("MQTT_QOS"), 0),
            mqtt_reconnect_min_s=_as_float(os.environ.get("MQTT_RECONNECT_MIN_S"), 1.0),
            mqtt_reconnect_max_s=_as_float(os.environ.get("MQTT_RECONNECT_MAX_S"), 30.0),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", shared.kafka_brokers),
            rfid_topic=os.environ.get("RFID_TOPIC", KAFKA_RFID_TOPIC),
            anpr_topic=os.environ.get("ANPR_TOPIC", KAFKA_ANPR_TOPIC),
            confirmed_topic=os.environ.get("CONFIRMED_TOPIC", KAFKA_CONFIRMED_TOPIC),
            postgres_dsn=os.environ.get("POSTGRES_DSN_LIBPQ", default_dsn),
            correlation_window_s=_as_float(os.environ.get("RFID_CORRELATION_WINDOW_S"), 5.0),
            correlator_confidence=_as_float(os.environ.get("RFID_CONFIRM_CONFIDENCE"), 0.97),
            correlator_group=os.environ.get("RFID_CORRELATOR_GROUP", "rfid-correlator"),
            consumer_group=os.environ.get("RFID_CONSUMER_GROUP", "rfid-consumer"),
            metrics_port=_as_int(os.environ.get("METRICS_PORT"), 9102),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            seed=_as_int(os.environ.get("RFID_SEED"), 42),
        )

    @property
    def num_readers(self) -> int:
        return self.num_gate_readers + self.num_corridor_readers
