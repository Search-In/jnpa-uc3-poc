"""Prometheus metrics for the identity (face-recognition) verifier.

A single counter keyed by the verification ``decision`` (VERIFIED / PROVISIONAL
/ REJECTED) is enough for the dashboard to show the match-rate and the
PROVISIONAL (admit-on-trust) rate side by side.
"""
from __future__ import annotations

from prometheus_client import Counter, make_asgi_app

VERIFICATIONS = Counter(
    "identity_verifications_total",
    "Total driver face-verification attempts, by decision.",
    ["decision"],   # VERIFIED | PROVISIONAL | REJECTED
)


def metrics_asgi_app():
    """Return an ASGI app exposing /metrics, mountable on the FastAPI app."""
    return make_asgi_app()
