"""Prometheus metrics for the parking-availability service.

Surfaces the two gauges the dashboard's parking-availability board scrapes: the
total free spaces across all facilities inside the geo-fenced port, and how many
facilities are currently FULL. Both are refreshed from the deterministic
occupancy model whenever the board is rendered.
"""
from __future__ import annotations

from prometheus_client import Gauge, make_asgi_app

PARKING_AVAILABLE = Gauge(
    "parking_available_total",
    "Total available (free) parking spaces across all port facilities.",
)

PARKING_FULL_FACILITIES = Gauge(
    "parking_full_facilities",
    "Number of parking facilities currently FULL (<5% free).",
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
