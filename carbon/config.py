"""Service configuration for the carbon-emissions calculator.

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
class CarbonConfig:
    # --- Synthetic AoI fleet (deterministic; used by /rollup) ---
    aoi_fleet_size: int = 200

    # --- Service identity (for jnpa.services registry / dashboards) ---
    service_name: str = "carbon"
    service_kind: str = "calc"
    base_url: str = "http://carbon:8340"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8340

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `carbon:8340`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "CarbonConfig":
        # Touch shared settings so a misconfigured environment fails fast and
        # consistently with the other services (mirrors vahan_sim).
        get_settings()
        return cls(
            aoi_fleet_size=_as_int(os.environ.get("CARBON_AOI_FLEET_SIZE"), 200),
            service_name=os.environ.get("CARBON_SERVICE_NAME", "carbon"),
            service_kind=os.environ.get("CARBON_SERVICE_KIND", "calc"),
            base_url=os.environ.get("CARBON_BASE_URL", "http://carbon:8340"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8340),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
