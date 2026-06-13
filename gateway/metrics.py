"""Prometheus metrics for the API gateway.

Mounted on the FastAPI app at /metrics. The `decision_path` label lets a single
dashboard panel show which fallback rung served each request per API.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

REQUESTS = Counter(
    "gateway_requests_total",
    "Total gateway requests by API and outcome.",
    ["api", "result"],   # result: ok | error | not_found | invalid
)

DECISIONS = Counter(
    "gateway_decisions_total",
    "Fallback decisions taken, by API and the chosen path.",
    ["api", "decision_path"],   # e.g. LIVE_PRIMARY, CACHED, PROVISIONAL ...
)

UPSTREAM_LATENCY = Histogram(
    "gateway_upstream_latency_seconds",
    "Latency of upstream calls made by the gateway.",
    ["api", "target"],
    buckets=(0.01, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0),
)

SOURCE_STATE = Gauge(
    "gateway_source_state",
    "Current degradation state per source (0=LIVE,1=DEGRADED,2=DOWN).",
    ["source"],
)

WS_CLIENTS = Gauge(
    "gateway_ws_clients",
    "Currently connected /api/ws clients.",
)

PROVISIONAL = Counter(
    "gateway_provisional_vehicles_total",
    "Vehicles admitted under the provisional 24h cure window.",
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
