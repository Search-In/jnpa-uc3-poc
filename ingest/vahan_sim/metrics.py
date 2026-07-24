"""Prometheus metrics for the Vahan simulator + live adapter.

Same metric names are used by both services (distinguished by the `service`
label) so a single dashboard panel covers sim and live.
"""
from __future__ import annotations

from prometheus_client import Counter, Histogram, make_asgi_app

LOOKUPS = Counter(
    "vahan_lookups_total",
    "Total Vahan/Sarathi/FASTag lookups.",
    ["service", "endpoint", "result"],   # result: hit | miss | invalid | error | disabled
)

LATENCY = Histogram(
    "vahan_lookup_latency_seconds",
    "End-to-end lookup latency including simulated/upstream delay.",
    ["service", "endpoint"],
    buckets=(0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0, 2.0),
)

WRITEBACKS = Counter(
    "vahan_vehicle_master_writebacks_total",
    "Total upserts into core.vehicle_rc from successful RC lookups.",
    ["service", "result"],   # ok | error
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
