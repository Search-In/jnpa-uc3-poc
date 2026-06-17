"""Prometheus metrics for the empty-container supply-demand optimiser.

A single dashboard panel covers the optimiser via these two series: how many
probable allocations were produced (split by cargo variant) and how much demand
is currently open.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, make_asgi_app

ALLOCATIONS = Counter(
    "empty_container_allocations_total",
    "Total probable allocations produced by the optimiser.",
    ["cargo_type"],   # container | oil_tanker | break_bulk | cement_bowser
)

OPEN_DEMAND = Gauge(
    "empty_container_open_demand",
    "Number of open empty-container demands awaiting allocation.",
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
