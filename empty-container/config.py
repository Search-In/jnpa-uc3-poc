"""Service configuration for the empty-container supply-demand optimiser.

Reads from the process environment (compose / .env.local), falling back to PoC
defaults so the service runs out of the box. Mirrors the shape of
``ingest/vahan_sim/config.py``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jnpa_shared.config import get_settings


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class OptimizerConfig:
    # --- Books ---
    demand_count: int = 40

    # --- Service identity (for core.ulip_service registry) ---
    service_name: str = "empty-container"
    service_kind: str = "optimizer"
    base_url: str = "http://empty-container:8330"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8330

    # --- DB ---
    postgres_dsn: str = ""

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `empty-container:8330`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "OptimizerConfig":
        shared = get_settings()
        return cls(
            demand_count=_as_int(os.environ.get("EMPTY_DEMAND_COUNT"), 40),
            service_name=os.environ.get("EMPTY_SERVICE_NAME", "empty-container"),
            service_kind=os.environ.get("EMPTY_SERVICE_KIND", "optimizer"),
            base_url=os.environ.get("EMPTY_BASE_URL", "http://empty-container:8330"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8330),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
