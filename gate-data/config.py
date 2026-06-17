"""Service configuration for the gate-data / Auto-LEO simulator.

Reads from the process environment (compose / .env.local), falling back to PoC
defaults so the service runs out of the box.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jnpa_shared.config import get_settings


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class GateConfig:
    # --- Dataset ---
    total_containers: int = 200

    # --- Auto-LEO reconciliation tolerances ---
    # Weighbridge measured weight vs Form-13 gross weight is flagged when the
    # relative discrepancy exceeds this fraction (2% by default).
    weight_tolerance_pct: float = 2.0

    # --- Service identity (for jnpa.services registry) ---
    service_name: str = "gate-data"
    service_kind: str = "sim"
    base_url: str = "http://gate-data:8350"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8350

    # --- DB ---
    postgres_dsn: str = ""

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `gate-data:8350`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "GateConfig":
        shared = get_settings()
        return cls(
            total_containers=_as_int(os.environ.get("GATE_TOTAL_CONTAINERS"), 200),
            weight_tolerance_pct=_as_float(os.environ.get("GATE_WEIGHT_TOLERANCE_PCT"), 2.0),
            service_name=os.environ.get("GATE_SERVICE_NAME", "gate-data"),
            service_kind=os.environ.get("GATE_SERVICE_KIND", "sim"),
            base_url=os.environ.get("GATE_BASE_URL", "http://gate-data:8350"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8350),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
