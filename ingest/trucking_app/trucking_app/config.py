"""Service configuration for the trucking-app telemetry simulator.

Reads from the process environment (the container gets these from compose /
.env.local) with PoC-sane defaults so the service runs out of the box. Mirrors
the env-parsing helpers used by the other ingest services (``ingest/rfid``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Tuple

from jnpa_shared.config import get_settings


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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


# --- Topic / table / topic-prefix constants (kept here so all parts agree) ---
MQTT_TELEMETRY_PREFIX = "trucks"               # trucks/{device_id}/telemetry
MQTT_TELEMETRY_SUFFIX = "telemetry"
MQTT_ETA_SUFFIX = "eta"
KAFKA_TELEMETRY_TOPIC = "truck.telemetry"
KAFKA_ETA_TOPIC = "truck.eta"
TELEMETRY_TABLE = "jnpa.truck_telemetry"
# Redis key the dashboard writes congestion to: traffic:segment:{id}:jam_factor
REDIS_JAM_KEY_FMT = "traffic:segment:{segment_id}:jam_factor"


@dataclass
class TruckConfig:
    # --- Fleet sizing ---
    num_devices: int = 20000        # Appendix B5: 20k concurrent installs
    max_devices: int = 30000        # scalable to 30k+
    seed: int = 1310                # deterministic fleet (distinct from rfid's 42)
    origin_radius_km: float = 100.0  # origins drawn within 100 km of a gate

    # --- Update cadence (seconds) ---
    interval_default_s: float = 5.0      # per-truck position update interval
    interval_at_gate_s: float = 2.0      # faster when AT_GATE_QUEUE
    eta_interval_s: float = 30.0         # ETA recompute + publish cadence
    db_flush_interval_s: float = 30.0    # batched COPY to Timescale every 30s
    osrm_refresh_eta_s: float = 30.0     # OSRM duration refresh for ETA

    # --- Speed model (km/h) ---
    speed_highway_kmh: float = 55.0      # free-flow on NH-348/348A
    speed_port_kmh: float = 25.0         # inside-port roads
    speed_noise_sigma_kmh: float = 4.0   # Gaussian speed noise
    # Jam factor (0..10) -> speed multiplier: v *= 1 - jam/10 * jam_sensitivity.
    jam_sensitivity: float = 0.9

    # --- GPS noise ---
    gps_sigma_m: float = 6.0             # epsilon ~ N(0, 6 m) on lat/lon
    gps_outlier_prob: float = 0.01       # 1% of pings
    gps_outlier_m: float = 50.0          # outlier displacement

    # --- State machine dwell times (seconds) ---
    gate_queue_dwell_s: float = 120.0    # base AT_GATE_QUEUE dwell (jam-scaled)
    inside_port_dwell_s: float = 300.0   # turnaround inside the port
    idle_dwell_s: float = 60.0           # rest at home before next trip

    # --- Routing ---
    osrm_base_url: str = "https://router.project-osrm.org/route/v1/driving/"
    osrm_timeout_s: float = 8.0
    here_api_key: str = ""
    here_base_url: str = "https://router.hereapi.com/v8/routes"
    routing_max_concurrency: int = 16    # cap concurrent route fetches
    route_cache_size: int = 4096         # LRU of OSRM polylines (origin->gate)

    # --- MQTT (aiomqtt) ---
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_keepalive: int = 30
    mqtt_qos_position: int = 0           # qos=0 for high-rate position updates
    mqtt_qos_state: int = 1              # qos=1 for state changes / ETA
    mqtt_reconnect_max_s: float = 30.0   # cap on reconnect backoff

    # --- Kafka ---
    kafka_brokers: str = "kafka:9092"
    telemetry_topic: str = KAFKA_TELEMETRY_TOPIC
    eta_topic: str = KAFKA_ETA_TOPIC

    # --- Postgres / Timescale (plain libpq DSN for asyncpg) ---
    postgres_dsn: str = "postgresql://postgres:jnpa_pw@postgres:5432/postgres"
    db_pool_min: int = 1
    db_pool_max: int = 4
    db_batch_max: int = 5000             # rows per COPY flush

    # --- Redis (jam_factor reads) ---
    redis_url: str = "redis://redis:6379/0"

    # --- Control plane / observability ---
    host: str = "0.0.0.0"
    port: int = 8240
    log_level: str = "INFO"
    use_uvloop: bool = True

    # --- Gate destinations (round-robin); ids mirror jnpa.gates seed rows ---
    gate_ids: Tuple[str, ...] = field(
        default_factory=lambda: ("G-NSICT", "G-JNPCT", "G-NSIGT", "G-BMCT")
    )

    @classmethod
    def from_env(cls) -> "TruckConfig":
        shared = get_settings()
        # Shared DSN is the SQLAlchemy "+asyncpg" form; asyncpg.connect() wants libpq.
        default_dsn = shared.postgres_dsn.replace("postgresql+asyncpg://", "postgresql://")
        return cls(
            num_devices=_as_int(os.environ.get("TRUCK_NUM_DEVICES"), 20000),
            max_devices=_as_int(os.environ.get("TRUCK_MAX_DEVICES"), 30000),
            seed=_as_int(os.environ.get("TRUCK_SEED"), 1310),
            origin_radius_km=_as_float(os.environ.get("TRUCK_ORIGIN_RADIUS_KM"), 100.0),
            interval_default_s=_as_float(os.environ.get("TRUCK_INTERVAL_S"), 5.0),
            interval_at_gate_s=_as_float(os.environ.get("TRUCK_INTERVAL_GATE_S"), 2.0),
            eta_interval_s=_as_float(os.environ.get("TRUCK_ETA_INTERVAL_S"), 30.0),
            db_flush_interval_s=_as_float(os.environ.get("TRUCK_DB_FLUSH_S"), 30.0),
            speed_highway_kmh=_as_float(os.environ.get("TRUCK_SPEED_HIGHWAY"), 55.0),
            speed_port_kmh=_as_float(os.environ.get("TRUCK_SPEED_PORT"), 25.0),
            speed_noise_sigma_kmh=_as_float(os.environ.get("TRUCK_SPEED_SIGMA"), 4.0),
            jam_sensitivity=_as_float(os.environ.get("TRUCK_JAM_SENSITIVITY"), 0.9),
            gps_sigma_m=_as_float(os.environ.get("TRUCK_GPS_SIGMA_M"), 6.0),
            gps_outlier_prob=_as_float(os.environ.get("TRUCK_GPS_OUTLIER_PROB"), 0.01),
            gps_outlier_m=_as_float(os.environ.get("TRUCK_GPS_OUTLIER_M"), 50.0),
            gate_queue_dwell_s=_as_float(os.environ.get("TRUCK_GATE_DWELL_S"), 120.0),
            inside_port_dwell_s=_as_float(os.environ.get("TRUCK_PORT_DWELL_S"), 300.0),
            idle_dwell_s=_as_float(os.environ.get("TRUCK_IDLE_DWELL_S"), 60.0),
            osrm_base_url=os.environ.get(
                "OSRM_BASE_URL", "https://router.project-osrm.org/route/v1/driving/"
            ),
            osrm_timeout_s=_as_float(os.environ.get("OSRM_TIMEOUT_S"), 8.0),
            here_api_key=os.environ.get("HERE_API_KEY", shared.here_api_key),
            routing_max_concurrency=_as_int(os.environ.get("TRUCK_ROUTE_CONCURRENCY"), 16),
            route_cache_size=_as_int(os.environ.get("TRUCK_ROUTE_CACHE"), 4096),
            mqtt_host=os.environ.get("MQTT_HOST", shared.mqtt_host),
            mqtt_port=_as_int(os.environ.get("MQTT_PORT"), shared.mqtt_port),
            mqtt_keepalive=_as_int(os.environ.get("MQTT_KEEPALIVE"), 30),
            mqtt_qos_position=_as_int(os.environ.get("MQTT_QOS_POSITION"), 0),
            mqtt_qos_state=_as_int(os.environ.get("MQTT_QOS_STATE"), 1),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", shared.kafka_brokers),
            telemetry_topic=os.environ.get("TRUCK_TELEMETRY_TOPIC", KAFKA_TELEMETRY_TOPIC),
            eta_topic=os.environ.get("TRUCK_ETA_TOPIC", KAFKA_ETA_TOPIC),
            postgres_dsn=os.environ.get("POSTGRES_DSN_LIBPQ", default_dsn),
            db_pool_min=_as_int(os.environ.get("TRUCK_DB_POOL_MIN"), 1),
            db_pool_max=_as_int(os.environ.get("TRUCK_DB_POOL_MAX"), 4),
            db_batch_max=_as_int(os.environ.get("TRUCK_DB_BATCH_MAX"), 5000),
            redis_url=os.environ.get("REDIS_URL", shared.redis_url),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8240),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            use_uvloop=_as_bool(os.environ.get("TRUCK_USE_UVLOOP"), True),
        )
