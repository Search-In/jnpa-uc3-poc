"""Service configuration for the congestion forecaster (ai/congestion).

Reads from the process environment (compose / .env.local) with PoC defaults so
the service runs out of the box on a CPU-only host. The model, feature window,
training and external-adapter knobs all live here so train.py and infer.py share
one source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

_HERE = Path(__file__).resolve().parent


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


@dataclass
class CongestionConfig:
    # --- API ---
    host: str = "0.0.0.0"
    port: int = 8311

    # --- Model topology ---
    # Node input feature count (see features.FEATURE_NAMES).
    in_features: int = 9
    gnn_hidden: int = 48          # GraphSAGE hidden width
    gnn_out: int = 24             # GraphSAGE output embedding per segment
    lstm_hidden: int = 64         # LSTM hidden width
    lstm_layers: int = 2

    # --- Temporal window / horizon ---
    window: int = 30              # input steps (each step = aggregate_s seconds)
    aggregate_s: int = 60         # one feature step per 60 s
    horizon_min: int = 15         # predict congestion onset within 15 min

    # Congestion label: a segment is "congested" when jam_factor >= this OR its
    # speed drops below congest_speed_kmh. jam_factor is HERE-style 0..10.
    congest_jam_factor: float = 6.0
    congest_speed_kmh: float = 18.0
    free_flow_speed_kmh: float = 55.0   # corridor design speed (for normalising)

    # --- Training ---
    epochs: int = 50
    # Consecutive windows overlap by (window-1)/window steps and are highly
    # autocorrelated. Validation is never strided. NOTE: stride=1 (use EVERY
    # training window) measurably improves the onset model — it lifts F1 from
    # 0.8411 (stride=4) to 0.8797 by cutting false positives 17->5, clearing the
    # >=0.85 bid gate. The ~4x compute cost is paid once at train time and the
    # weights are baked into the image, so inference is unaffected. Raise the
    # stride only for a quick smoke-train where the metric does not matter.
    train_stride: int = 1
    lr: float = 2.0e-3
    weight_decay: float = 1.0e-5
    batch_segments: int = 0       # 0 = full graph each step (small graph)
    base_class_weight: float = 3.0    # positive-class weight in BCE
    class_weight_step: float = 1.5    # multiplier when a retry is needed
    max_retries: int = 2          # extra training passes if metrics under target
    val_hours: int = 24           # held-out tail used for metrics
    seed: int = 1337

    # --- Target metrics (printed + gated at end of train.py) ---
    target_f1: float = 0.85
    target_precision: float = 0.80
    target_recall: float = 0.80

    # --- Synthetic bootstrap history ---
    history_days: int = 14
    fast_forward: int = 5         # "5x real-time" headline (documented; gen is offline)

    # --- Persistence ---
    weights_dir: str = str(_HERE / "artifacts")
    weights_name: str = "congestion_gnn_lstm.pt"
    metrics_name: str = "metrics.json"
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "models"
    minio_prefix: str = "congestion/"
    minio_secure: bool = False

    # --- Postgres (feature backfill source + store) ---
    # libpq DSN (no SQLAlchemy "+asyncpg" prefix) for asyncpg.
    postgres_dsn_libpq: str = "postgresql://postgres:jnpa_pw@postgres:5432/postgres"

    # --- Redis (external-source speed cache) ---
    redis_url: str = "redis://redis:6379/0"

    # --- Kafka (continuous prediction publishing) ---
    kafka_brokers: str = "kafka:9092"
    predictions_topic: str = "traffic.predictions"
    publish_interval_s: int = 60   # background scheduler cadence

    # --- External traffic adapters (sources/) ---
    google_maps_api_key: str = ""
    here_api_key: str = ""
    tomtom_api_key: str = ""
    source_timeout_s: float = 1.0  # per-source timeout
    source_cache_ttl_s: int = 90   # Redis cache TTL for a segment speed
    source_order: List[str] = field(default_factory=lambda: ["google", "here", "tomtom"])

    log_level: str = "INFO"

    @property
    def weights_path(self) -> str:
        return str(Path(self.weights_dir) / self.weights_name)

    @property
    def metrics_path(self) -> str:
        return str(Path(self.weights_dir) / self.metrics_name)

    @property
    def weights_key(self) -> str:
        return f"{self.minio_prefix}{self.weights_name}"

    @property
    def metrics_key(self) -> str:
        return f"{self.minio_prefix}{self.metrics_name}"

    @classmethod
    def from_env(cls) -> "CongestionConfig":
        order = os.environ.get("CONGESTION_SOURCE_ORDER")
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8311),
            window=_as_int(os.environ.get("CONGESTION_WINDOW"), 30),
            aggregate_s=_as_int(os.environ.get("CONGESTION_AGGREGATE_S"), 60),
            horizon_min=_as_int(os.environ.get("CONGESTION_HORIZON_MIN"), 15),
            congest_jam_factor=_as_float(os.environ.get("CONGESTION_JAM_FACTOR"), 6.0),
            congest_speed_kmh=_as_float(os.environ.get("CONGESTION_SPEED_KMH"), 18.0),
            epochs=_as_int(os.environ.get("CONGESTION_EPOCHS"), 50),
            train_stride=_as_int(os.environ.get("CONGESTION_TRAIN_STRIDE"), 1),
            lr=_as_float(os.environ.get("CONGESTION_LR"), 2.0e-3),
            base_class_weight=_as_float(os.environ.get("CONGESTION_CLASS_WEIGHT"), 3.0),
            max_retries=_as_int(os.environ.get("CONGESTION_MAX_RETRIES"), 2),
            history_days=_as_int(os.environ.get("CONGESTION_HISTORY_DAYS"), 14),
            val_hours=_as_int(os.environ.get("CONGESTION_VAL_HOURS"), 24),
            seed=_as_int(os.environ.get("CONGESTION_SEED"), 1337),
            target_f1=_as_float(os.environ.get("CONGESTION_TARGET_F1"), 0.85),
            target_precision=_as_float(os.environ.get("CONGESTION_TARGET_PRECISION"), 0.80),
            target_recall=_as_float(os.environ.get("CONGESTION_TARGET_RECALL"), 0.80),
            weights_dir=os.environ.get("CONGESTION_WEIGHTS_DIR", str(_HERE / "artifacts")),
            minio_endpoint=os.environ.get("MINIO_ENDPOINT", "minio:9000"),
            minio_access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            minio_bucket=os.environ.get("CONGESTION_MODEL_BUCKET", "models"),
            minio_prefix=os.environ.get("CONGESTION_MODEL_PREFIX", "congestion/"),
            minio_secure=_as_bool(os.environ.get("MINIO_SECURE"), False),
            postgres_dsn_libpq=os.environ.get(
                "CONGESTION_POSTGRES_DSN",
                "postgresql://postgres:jnpa_pw@postgres:5432/postgres",
            ),
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", "kafka:9092"),
            predictions_topic=os.environ.get("CONGESTION_PREDICTIONS_TOPIC", "traffic.predictions"),
            publish_interval_s=_as_int(os.environ.get("CONGESTION_PUBLISH_INTERVAL_S"), 60),
            google_maps_api_key=os.environ.get("GOOGLE_MAPS_API_KEY", ""),
            here_api_key=os.environ.get("HERE_API_KEY", ""),
            tomtom_api_key=os.environ.get("TOMTOM_API_KEY", ""),
            source_timeout_s=_as_float(os.environ.get("CONGESTION_SOURCE_TIMEOUT_S"), 1.0),
            source_cache_ttl_s=_as_int(os.environ.get("CONGESTION_SOURCE_CACHE_TTL_S"), 90),
            source_order=[s.strip() for s in order.split(",")] if order else ["google", "here", "tomtom"],
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
