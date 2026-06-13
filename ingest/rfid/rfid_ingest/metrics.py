"""Prometheus metrics for the RFID services.

One module shared by the emulator, consumer, and correlator (they each touch a
subset). Names are stable so dashboards/alerts can rely on them. Exposition is
on the configured ``METRICS_PORT`` (default 9102).
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server

# --- Emulator ---
RFID_PUBLISHED = Counter(
    "rfid_reads_published_total",
    "Total RFID reads published to MQTT by the emulator.",
    ["reader_id"],
)
MQTT_PUBLISH_ERRORS = Counter(
    "rfid_mqtt_publish_errors_total",
    "Total MQTT publish failures (queued while disconnected, etc.).",
)
ACTIVE_READERS = Gauge(
    "rfid_active_readers",
    "Number of logical readers the emulator is driving.",
)

# --- Consumer ---
RFID_CONSUMED = Counter(
    "rfid_reads_consumed_total",
    "Total RFID reads received from MQTT by the consumer.",
)
RFID_PERSISTED = Counter(
    "rfid_reads_persisted_total",
    "Total RFID reads written to jnpa.rfid_reads.",
)
RFID_VALIDATION_ERRORS = Counter(
    "rfid_validation_errors_total",
    "Total MQTT payloads that failed RfidRead schema validation.",
)
RFID_FORWARDED = Counter(
    "rfid_reads_forwarded_total",
    "Total RFID reads forwarded to the Kafka rfid.reads topic.",
)

# --- Correlator ---
ANPR_SEEN = Counter(
    "correlator_anpr_seen_total",
    "Total anpr.reads consumed by the correlator.",
)
RFID_SEEN = Counter(
    "correlator_rfid_seen_total",
    "Total rfid.reads consumed by the correlator.",
)
VEHICLE_CONFIRMED = Counter(
    "vehicle_confirmed_total",
    "Total vehicle.confirmed events emitted (rfid<->anpr match within window).",
    ["gate_id"],
)

KAFKA_ERRORS = Counter(
    "rfid_kafka_errors_total",
    "Total Kafka produce/delivery errors across RFID services.",
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus exposition HTTP server (idempotent per process)."""
    start_http_server(port)


def counter_total(counter: Counter) -> float:
    total = 0.0
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total
