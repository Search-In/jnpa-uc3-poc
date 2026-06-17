"""Prometheus metrics for the carbon-emissions calculator.

A single dashboard panel reads the AoI total gauge and the estimate counter so
the C6 emissions figure on the dashboard is scrape-backed.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, make_asgi_app

AOI_TOTAL_KG = Gauge(
    "carbon_aoi_total_kg",
    "Total CO2e (kg) for the trailer fleet currently in the Area of Interest.",
)

ESTIMATES = Counter(
    "carbon_estimates_total",
    "Total /estimate emission calculations served.",
    ["vehicle_class"],
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
