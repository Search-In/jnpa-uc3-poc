"""Prometheus metrics for the gate-data / Auto-LEO service.

Surfaces the two counters the dashboard's Auto-LEO and Customs panels scrape:
how many reconciliations resolved ready vs. blocked, and how many of each kind
of Customs flag has fired.
"""
from __future__ import annotations

from prometheus_client import Counter, make_asgi_app

RECONCILIATIONS = Counter(
    "leo_reconciliations_total",
    "Total Auto-LEO reconciliations performed.",
    ["result"],   # ready | blocked
)

CUSTOMS_FLAGS = Counter(
    "customs_flags_total",
    "Total Customs flags raised during Auto-LEO reconciliation.",
    ["flag"],     # ESEAL_TAMPER | WEIGHT_MISMATCH | LEO_MISSING | ID_MISMATCH | ...
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
