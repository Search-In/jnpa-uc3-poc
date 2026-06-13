"""Prometheus metrics for anpr-ingest.

Exposes the counters required by the spec on `/metrics` (default container
port 9101). The names are intentionally stable so dashboards/alerts can rely
on them.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, start_http_server

FRAMES_PROCESSED = Counter(
    "frames_processed_total",
    "Total video frames processed across all camera feeds.",
    ["camera_id"],
)
PLATES_EMITTED = Counter(
    "plates_emitted_total",
    "Total AnprRead events emitted to Kafka.",
    ["camera_id"],
)
KAFKA_ERRORS = Counter(
    "kafka_errors_total",
    "Total Kafka produce/delivery errors.",
)
WEATHER_PULLS = Counter(
    "weather_pulls_total",
    "Total OpenWeatherMap (or fallback) weather refreshes.",
    ["result"],  # ok | error | skipped
)
NO_FEED_EVENTS = Counter(
    "no_feed_events_total",
    "Total no_feed health events emitted (zero clips available).",
)
ACTIVE_FEEDS = Gauge(
    "active_feeds",
    "Number of clip feeds currently being replayed.",
)


def start_metrics_server(port: int) -> None:
    """Start the Prometheus exposition HTTP server (idempotent per process)."""
    start_http_server(port)


def counter_total(counter: Counter) -> float:
    """Sum a (possibly labelled) Counter's current value across all label sets."""
    total = 0.0
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total


def snapshot() -> dict:
    """Current counter totals — emitted to logs so operators can grep them."""
    return {
        "frames_processed_total": counter_total(FRAMES_PROCESSED),
        "plates_emitted_total": counter_total(PLATES_EMITTED),
        "kafka_errors_total": counter_total(KAFKA_ERRORS),
        "weather_pulls_total": counter_total(WEATHER_PULLS),
        "no_feed_events_total": counter_total(NO_FEED_EVENTS),
    }
