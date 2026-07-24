"""Service configuration for the parking-availability service.

Reads from the process environment (compose / .env.local), falling back to PoC
defaults so the service runs out of the box.
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
class ParkingConfig:
    # --- Service identity (for core.ulip_service registry) ---
    service_name: str = "parking"
    service_kind: str = "sim"
    base_url: str = "http://parking:8370"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8370

    # --- DB ---
    postgres_dsn: str = ""

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `parking:8370`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ParkingConfig":
        shared = get_settings()
        return cls(
            service_name=os.environ.get("PARKING_SERVICE_NAME", "parking"),
            service_kind=os.environ.get("PARKING_SERVICE_KIND", "sim"),
            base_url=os.environ.get("PARKING_BASE_URL", "http://parking:8370"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8370),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
