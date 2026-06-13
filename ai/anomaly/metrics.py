"""Prometheus metrics for the behavioural anomaly detector.

Exposed on ``/metrics`` (mounted ASGI app on the FastAPI service). Names are
stable so dashboards/alerts can rely on them.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

TRACKS_PROCESSED = Counter(
    "anomaly_tracks_processed_total",
    "Total tracks evaluated by the engine.",
    ["source"],   # bytetrack | telemetry | synthetic
)
ALERTS_RAISED = Counter(
    "anomaly_alerts_raised_total",
    "Total alerts raised, by kind.",
    ["kind", "severity"],
)
FRAMES_CONSUMED = Counter(
    "anomaly_frames_consumed_total",
    "Total frame-bus frames consumed.",
    ["camera_id"],
)
EVIDENCE_STORED = Counter(
    "anomaly_evidence_stored_total",
    "Total evidence images stored to MinIO.",
)
AE_TRAININGS = Counter(
    "anomaly_ae_trainings_total",
    "Total autoencoder (re)training runs.",
    ["result"],   # ok | skipped | error
)
AE_THRESHOLD = Gauge(
    "anomaly_ae_threshold",
    "Current autoencoder reconstruction-error anomaly threshold.",
)
ACTIVE_TRACKS = Gauge(
    "anomaly_active_tracks",
    "Tracks currently open across all cameras.",
)
EVAL_LATENCY = Histogram(
    "anomaly_eval_seconds",
    "Per-track engine evaluation latency.",
)


def metrics_asgi_app():
    """ASGI app for the Prometheus exposition (mounted at /metrics)."""
    return make_asgi_app()


def snapshot() -> dict:
    """Current counter totals — logged so operators can grep them."""
    def _total(counter: Counter) -> float:
        total = 0.0
        for metric in counter.collect():
            for sample in metric.samples:
                if sample.name.endswith("_total"):
                    total += sample.value
        return total

    return {
        "tracks_processed_total": _total(TRACKS_PROCESSED),
        "alerts_raised_total": _total(ALERTS_RAISED),
        "frames_consumed_total": _total(FRAMES_CONSUMED),
        "evidence_stored_total": _total(EVIDENCE_STORED),
    }


__all__ = [
    "TRACKS_PROCESSED",
    "ALERTS_RAISED",
    "FRAMES_CONSUMED",
    "EVIDENCE_STORED",
    "AE_TRAININGS",
    "AE_THRESHOLD",
    "ACTIVE_TRACKS",
    "EVAL_LATENCY",
    "metrics_asgi_app",
    "snapshot",
]
