"""Configuration for the scenarios-runner (Sub-Criterion 5).

Reads the same environment the other services use (compose / .env.local) with
PoC defaults so the runner works out of the box on the jnpa network.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from jnpa_shared.config import get_settings


def _int(v: str | None, d: int) -> int:
    try:
        return int(v) if v is not None else d
    except (TypeError, ValueError):
        return d


@dataclass
class ScenarioConfig:
    host: str = "0.0.0.0"
    port: int = 8400

    # Upstream services (jnpa network names).
    gateway_url: str = "http://gateway:8000"
    truck_api_url: str = "http://truck-sim:8240"
    congestion_url: str = "http://congestion:8311"
    anomaly_url: str = "http://anomaly:8321"

    # Infra.
    postgres_dsn: str = ""
    redis_url: str = ""
    kafka_brokers: str = ""

    # Tunables (kept short so the verification curl returns quickly; the reactive
    # downstream effects keep running via the injected trucks / forecaster).
    upstream_timeout_s: float = 5.0
    # How long to wait for the forecaster to reflect an injected build-up before
    # recording the assertion as met/degraded (best-effort, per design decision).
    forecast_poll_attempts: int = 6
    forecast_poll_interval_s: float = 2.0

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "ScenarioConfig":
        s = get_settings()
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_int(os.environ.get("PORT"), 8400),
            gateway_url=os.environ.get("SCENARIOS_GATEWAY_URL", "http://gateway:8000"),
            truck_api_url=os.environ.get("SCENARIOS_TRUCK_URL", "http://truck-sim:8240"),
            congestion_url=os.environ.get("SCENARIOS_CONGESTION_URL", "http://congestion:8311"),
            anomaly_url=os.environ.get("SCENARIOS_ANOMALY_URL", "http://anomaly:8321"),
            postgres_dsn=os.environ.get("POSTGRES_DSN", s.postgres_dsn),
            redis_url=os.environ.get("REDIS_URL", s.redis_url),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", s.kafka_brokers),
            forecast_poll_attempts=_int(os.environ.get("SCENARIOS_FORECAST_ATTEMPTS"), 6),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


__all__ = ["ScenarioConfig"]
