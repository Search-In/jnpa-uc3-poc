"""Application configuration.

Loads settings from the process environment, falling back to a `.env.local`
file at the repository root. Every service imports `get_settings()` to obtain
a cached `Settings` instance.

Service names (``postgres``, ``kafka``, ``redis``, ``mosquitto`` …) resolve on
the docker ``jnpa`` network. Code running on the *host* (e.g. the bootstrap
self-test) should set the matching env vars to ``localhost`` first; the helper
``Settings.for_host()`` does this rewrite in one call.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _find_env_file() -> str:
    """Walk upward from CWD looking for a `.env.local`; default to './.env.local'."""
    here = Path.cwd()
    for candidate in [here, *here.parents]:
        env = candidate / ".env.local"
        if env.is_file():
            return str(env)
    return ".env.local"


class Settings(BaseSettings):
    """Typed view over the PoC environment."""

    model_config = SettingsConfigDict(
        env_file=_find_env_file(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Postgres / Timescale ---
    postgres_password: str = "jnpa_pw"
    postgres_dsn: str = "postgresql+asyncpg://postgres:jnpa_pw@postgres:5432/postgres"

    # --- Redis ---
    redis_url: str = "redis://redis:6379/0"

    # --- Kafka ---
    kafka_brokers: str = "kafka:9092"

    # --- MQTT ---
    mqtt_broker: str = "mosquitto:1883"

    # --- MinIO ---
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"

    # --- External APIs (optional; blank until provisioned) ---
    google_maps_api_key: str = ""
    here_api_key: str = ""
    tomtom_api_key: str = ""
    openweather_api_key: str = ""
    surepass_api_token: str = ""
    ulip_api_key: str = ""
    bhuvan_api_key: str = ""

    # --- Corridor geometry ---
    corridor_name: str = "NH-348 JNPA to Karal Phata"
    port_lat: float = 18.9489
    port_lon: float = 72.9492
    karal_lat: float = 18.78
    karal_lon: float = 73.08

    # --- Observability ---
    trace_id: str = Field(default="local-dev")

    # ----------------------------------------------------------------- helpers
    @property
    def mqtt_host(self) -> str:
        return self.mqtt_broker.split(":", 1)[0]

    @property
    def mqtt_port(self) -> int:
        parts = self.mqtt_broker.split(":", 1)
        return int(parts[1]) if len(parts) == 2 else 1883

    @property
    def kafka_first_broker(self) -> str:
        return self.kafka_brokers.split(",", 1)[0].strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached `Settings` instance."""
    return Settings()


# Constant re-exports for convenience in modules that prefer plain names.
SETTINGS = get_settings()
KAFKA_BROKERS = SETTINGS.kafka_brokers
POSTGRES_DSN = SETTINGS.postgres_dsn
REDIS_URL = SETTINGS.redis_url
MQTT_BROKER = SETTINGS.mqtt_broker

# JNPA corridor endpoints, also exposed as plain constants.
PORT_LAT = SETTINGS.port_lat
PORT_LON = SETTINGS.port_lon
KARAL_LAT = SETTINGS.karal_lat
KARAL_LON = SETTINGS.karal_lon
CORRIDOR_NAME = SETTINGS.corridor_name


def _env_or(name: str, default: str) -> str:  # pragma: no cover - trivial
    return os.environ.get(name, default)
