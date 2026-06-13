"""Service configuration for the Vahan/Sarathi/FASTag simulator.

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
class SimConfig:
    # --- Dataset ---
    total_plates: int = 25_000
    fixture_path: str = "/data/fixtures/known_plates.json"
    fixture_count: int = 50

    # --- Artificial latency (mimic Parivahan): mean +/- jitter, milliseconds ---
    latency_mean_ms: float = 100.0
    latency_jitter_ms: float = 50.0

    # --- Service identity (for jnpa.services registry) ---
    service_name: str = "vahan"
    service_kind: str = "sim"
    base_url: str = "http://vahan-sim:8201"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8201

    # --- DB ---
    postgres_dsn: str = ""

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `vahan-sim:8201`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "SimConfig":
        shared = get_settings()
        return cls(
            total_plates=_as_int(os.environ.get("VAHAN_TOTAL_PLATES"), 25_000),
            fixture_path=os.environ.get("VAHAN_FIXTURE_PATH", "/data/fixtures/known_plates.json"),
            fixture_count=_as_int(os.environ.get("VAHAN_FIXTURE_COUNT"), 50),
            latency_mean_ms=_as_float(os.environ.get("VAHAN_LATENCY_MEAN_MS"), 100.0),
            latency_jitter_ms=_as_float(os.environ.get("VAHAN_LATENCY_JITTER_MS"), 50.0),
            service_name=os.environ.get("VAHAN_SERVICE_NAME", "vahan"),
            service_kind=os.environ.get("VAHAN_SERVICE_KIND", "sim"),
            base_url=os.environ.get("VAHAN_BASE_URL", "http://vahan-sim:8201"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8201),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
