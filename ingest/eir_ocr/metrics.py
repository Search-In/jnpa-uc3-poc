"""Prometheus metrics for the EIR OCR service."""
from __future__ import annotations

from prometheus_client import Counter, Histogram, make_asgi_app

INFER = Counter(
    "eir_ocr_infer_total",
    "Total EIR OCR infer requests.",
    ["result"],  # ok | error | cache_hit
)

LATENCY = Histogram(
    "eir_ocr_infer_latency_seconds",
    "End-to-end infer latency including OCR + extract.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

CACHE = Counter(
    "eir_ocr_cache_total",
    "OCR result cache hits/misses.",
    ["result"],  # hit | miss
)


def metrics_asgi_app():
    return make_asgi_app()
