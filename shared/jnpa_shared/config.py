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

    # --- Determinism / demo mode ---
    # A single global seed makes the whole simulated system reproducible: each
    # component derives its own stream seed from this via ``derive_seed`` so a
    # recorded runbook replays byte-identically. Per-component seeds (e.g.
    # ``truck.config.seed``) still exist for backward compatibility but should
    # default to ``derive_seed(<component>)`` so one knob drives them all.
    seed: int = 1337

    # DATA_MODE selects the dominant data source for the *whole* twin. ``mock``
    # is offline-first (no external network); ``live`` lets connectors reach
    # external APIs when keys are present. The frontend reads VITE_DATA_MODE; this
    # is the backend counterpart so the self-test can assert an offline run.
    data_mode: str = "mock"

    # When true, simulators must not touch the network even if keys are present
    # (used by the network-disabled acceptance run). Implies ``data_mode=mock``.
    offline: bool = False

    # CloudEvents envelope toggle. When true, sim/connector events are wrapped in
    # a CloudEvents 1.0 structured-mode envelope carrying ``sourcesystem`` and
    # ``rawref`` extensions so the dashboard can distinguish SIM from LIVE while
    # the payload still flows through the real pipeline unchanged.
    cloudevents_enabled: bool = True

    # --- Fleet scale (statistical fleet; never instantiate N objects/tick) ---
    truck_num_devices: int = 20000
    truck_max_devices: int = 30000

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

    @property
    def is_offline(self) -> bool:
        """True when the twin must run network-disabled (offline implies mock)."""
        return self.offline or self.data_mode.lower() == "mock"

    def derive_seed(self, component: str) -> int:
        """Derive a stable per-component seed from the single global ``seed``.

        Same global ``seed`` + same ``component`` name → same value across runs
        and processes, so every simulator replays identically under one knob.
        Uses a hash (not Python's salted ``hash``) so it is stable across
        interpreter restarts.
        """
        import hashlib

        h = hashlib.sha256(f"{self.seed}:{component}".encode("utf-8")).hexdigest()
        # 32-bit unsigned — wide enough for random.Random / numpy seeding.
        return int(h[:8], 16)


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
