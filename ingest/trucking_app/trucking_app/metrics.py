"""Prometheus metrics for the trucking-app telemetry simulator.

Names are stable so dashboards/alerts can rely on them. Exposition is mounted
onto the FastAPI control plane at ``/metrics`` (see ``app.py``), matching the
vahan-sim pattern, so there is no separate metrics port to manage.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

# --- Fleet population / state ---
TRUCKS_TOTAL = Gauge(
    "truck_devices_total",
    "Current number of simulated truck devices in the fleet.",
)
TRUCKS_BY_STATE = Gauge(
    "truck_devices_state",
    "Number of trucks currently in each state-machine state.",
    ["state"],
)

# --- Telemetry publishing ---
TELEMETRY_PUBLISHED = Counter(
    "truck_telemetry_published_total",
    "Total telemetry pings published, by sink.",
    ["sink"],   # mqtt | kafka
)
ETA_PUBLISHED = Counter(
    "truck_eta_published_total",
    "Total ETA messages published, by sink.",
    ["sink"],
)
TELEMETRY_PERSISTED = Counter(
    "truck_telemetry_persisted_total",
    "Total telemetry rows written to core.truck_telemetry via batched COPY.",
)
PUBLISH_ERRORS = Counter(
    "truck_publish_errors_total",
    "Total publish/persist errors, by sink.",
    ["sink"],   # mqtt | kafka | db
)
STATE_TRANSITIONS = Counter(
    "truck_state_transitions_total",
    "Total truck state-machine transitions.",
    ["to_state"],
)

# --- Routing ---
ROUTES_FETCHED = Counter(
    "truck_routes_fetched_total",
    "Total routes obtained, by provider.",
    ["provider"],   # osrm | here | deadreckon
)
ROUTE_FETCH_SECONDS = Histogram(
    "truck_route_fetch_seconds",
    "Wall-clock seconds to obtain a route.",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0),
)

# --- Throughput / loop health ---
PUBLISH_RATE = Gauge(
    "truck_publish_rate_msgs_per_sec",
    "Rolling telemetry publish rate (msgs/sec) over the last stats window.",
)
DB_QUEUE_DEPTH = Gauge(
    "truck_db_queue_depth",
    "Telemetry rows buffered awaiting the next batched COPY flush.",
)


def metrics_asgi_app():
    """Return an ASGI app exposing the default Prometheus registry."""
    return make_asgi_app()


def counter_total(counter: Counter) -> float:
    total = 0.0
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total"):
                total += sample.value
    return total
