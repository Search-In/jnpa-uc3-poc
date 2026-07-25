"""Service configuration for the behavioural anomaly detector (ai/anomaly).

Reads from the process environment (compose / .env.local) with PoC defaults so
the service runs out of the box on a CPU-only host with no torch and no
supervision/ultralytics installed (it then degrades to rules-only on injected /
telemetry-derived tracks). Every threshold the rules and the autoencoder use is
declared here so the engine, the rules, and the AE share one source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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
class AnomalyConfig:
    # --- API ---
    host: str = "0.0.0.0"
    port: int = 8321

    # --- ByteTrack / detector ---
    yolo_weights: str = "yolov8n.pt"          # vehicle detector (COCO)
    detect_conf: float = 0.25
    track_activation_threshold: float = 0.25   # supervision ByteTrack high thr
    lost_track_buffer: int = 30                # frames a track survives occlusion
    minimum_matching_threshold: float = 0.8
    frame_rate: int = 5                        # frame-bus fps (matches ingest)

    # --- Stationary / motion thresholds ---
    # A track is "stationary" at a step when its speed is below this. Speeds are
    # in km/h (telemetry) or px/s mapped to km/h-equivalent for ByteTrack tracks.
    stationary_speed_kmh: float = 3.0
    # Min displacement (metres) over the dwell window to still count as moving.
    stationary_radius_m: float = 8.0

    # --- Wrong-way (rules/wrongway.py) ---
    # A track whose heading diverges from the camera's allowed bearing by more
    # than this for longer than the hold window -> WRONG_WAY.
    wrongway_divergence_deg: float = 120.0
    wrongway_hold_s: float = 2.0

    # --- Abandoned (rules/abandoned.py) ---
    # Stationary in a NON-parking polygon for longer than this -> ABANDONED.
    abandoned_dwell_s: float = 120.0

    # --- Illegal parking (rules/parking.py) ---
    # Stationary inside any NO_PARK_ZONES polygon for longer than this ->
    # ILLEGAL_PARKING, with duration-based escalation.
    parking_dwell_s: float = 300.0
    parking_warning_s: float = 300.0          # 5 min
    parking_critical_s: float = 900.0         # 15 min
    parking_police_s: float = 1800.0          # 30 min

    # --- Route deviation (rules/route_deviation.py) ---
    # Compare a truck's GPS path to its assigned route; cosine distance OR
    # off-route distance sustained beyond the hold window -> ROUTE_DEVIATION.
    route_cosine_threshold: float = 0.4
    route_offroute_m: float = 800.0
    route_hold_s: float = 90.0
    # Trucking-app control plane (assigned route lookup: /devices/{id}/route).
    truck_api_url: str = "http://truck-sim:8240"
    truck_api_timeout_s: float = 2.0

    # --- Autoencoder (autoencoder/model.py) ---
    ae_seq_len: int = 64                       # trajectory feature steps per track
    ae_features: int = 3                       # speed series, heading-sin/cos packed
    ae_latent: int = 8
    ae_epochs: int = 40
    ae_lr: float = 1.0e-3
    ae_batch: int = 32
    ae_train_days: int = 7                     # POST /train_ae window
    ae_min_tracks: int = 64                    # min tracks needed to (re)train
    ae_threshold_pct: float = 99.0             # recon-error percentile for the gate
    ae_seed: int = 1337

    # --- Frame bus (Redis Streams) ---
    redis_url: str = "redis://redis:6379/0"
    # Cameras whose frame streams we consume. Empty -> all corridor/gate cameras
    # discovered from the seed list below.
    frame_cameras: str = ""

    # --- Kafka (alert publishing) ---
    kafka_brokers: str = "kafka:9092"
    alerts_topic: str = "alerts"

    # --- Postgres (alert store + track history for AE training) ---
    # libpq DSN (no SQLAlchemy "+asyncpg" prefix) for psycopg.
    postgres_dsn_libpq: str = "postgresql://postgres:jnpa_pw@postgres:5432/postgres"

    # --- MinIO (AE weights + evidence images) ---
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_model_bucket: str = "models"
    minio_evidence_bucket: str = "evidence"
    minio_prefix: str = "anomaly/"
    minio_secure: bool = False
    # Public-facing base for evidence URLs attached to alerts.payload. Defaults
    # to the MinIO S3 endpoint; override to a CDN/gateway in production.
    evidence_url_base: str = ""

    # --- Persistence ---
    weights_dir: str = str(_HERE / "artifacts")
    weights_name: str = "trajectory_ae.pt"
    metrics_name: str = "ae_metrics.json"

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

    @property
    def evidence_base(self) -> str:
        """Base URL for evidence objects (no trailing slash)."""
        if self.evidence_url_base:
            return self.evidence_url_base.rstrip("/")
        scheme = "https" if self.minio_secure else "http"
        return f"{scheme}://{self.minio_endpoint}/{self.minio_evidence_bucket}"

    @property
    def cameras(self) -> list[str]:
        if self.frame_cameras.strip():
            return [c.strip() for c in self.frame_cameras.split(",") if c.strip()]
        return list(DEFAULT_CAMERAS)

    @classmethod
    def from_env(cls) -> "AnomalyConfig":
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8321),
            yolo_weights=os.environ.get("ANOMALY_YOLO_WEIGHTS", "yolov8n.pt"),
            detect_conf=_as_float(os.environ.get("ANOMALY_DETECT_CONF"), 0.25),
            track_activation_threshold=_as_float(
                os.environ.get("ANOMALY_TRACK_ACT_THR"), 0.25),
            lost_track_buffer=_as_int(os.environ.get("ANOMALY_LOST_BUFFER"), 30),
            frame_rate=_as_int(os.environ.get("ANOMALY_FRAME_RATE"), 5),
            stationary_speed_kmh=_as_float(os.environ.get("ANOMALY_STATIONARY_KMH"), 3.0),
            stationary_radius_m=_as_float(os.environ.get("ANOMALY_STATIONARY_RADIUS_M"), 8.0),
            wrongway_divergence_deg=_as_float(os.environ.get("ANOMALY_WRONGWAY_DEG"), 120.0),
            wrongway_hold_s=_as_float(os.environ.get("ANOMALY_WRONGWAY_HOLD_S"), 2.0),
            abandoned_dwell_s=_as_float(os.environ.get("ANOMALY_ABANDONED_S"), 120.0),
            parking_dwell_s=_as_float(os.environ.get("ANOMALY_PARKING_S"), 300.0),
            parking_warning_s=_as_float(os.environ.get("ANOMALY_PARKING_WARN_S"), 300.0),
            parking_critical_s=_as_float(os.environ.get("ANOMALY_PARKING_CRIT_S"), 900.0),
            parking_police_s=_as_float(os.environ.get("ANOMALY_PARKING_POLICE_S"), 1800.0),
            route_cosine_threshold=_as_float(os.environ.get("ANOMALY_ROUTE_COSINE"), 0.4),
            route_offroute_m=_as_float(os.environ.get("ANOMALY_ROUTE_OFFROUTE_M"), 800.0),
            route_hold_s=_as_float(os.environ.get("ANOMALY_ROUTE_HOLD_S"), 90.0),
            truck_api_url=os.environ.get("ANOMALY_TRUCK_API_URL", "http://truck-sim:8240"),
            truck_api_timeout_s=_as_float(os.environ.get("ANOMALY_TRUCK_API_TIMEOUT_S"), 2.0),
            ae_seq_len=_as_int(os.environ.get("ANOMALY_AE_SEQ_LEN"), 64),
            ae_epochs=_as_int(os.environ.get("ANOMALY_AE_EPOCHS"), 40),
            ae_lr=_as_float(os.environ.get("ANOMALY_AE_LR"), 1.0e-3),
            ae_train_days=_as_int(os.environ.get("ANOMALY_AE_TRAIN_DAYS"), 7),
            ae_min_tracks=_as_int(os.environ.get("ANOMALY_AE_MIN_TRACKS"), 64),
            ae_threshold_pct=_as_float(os.environ.get("ANOMALY_AE_THRESHOLD_PCT"), 99.0),
            ae_seed=_as_int(os.environ.get("ANOMALY_AE_SEED"), 1337),
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            frame_cameras=os.environ.get("ANOMALY_FRAME_CAMERAS", ""),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", "kafka:9092"),
            alerts_topic=os.environ.get("ANOMALY_ALERTS_TOPIC", "alerts"),
            postgres_dsn_libpq=os.environ.get(
                "ANOMALY_POSTGRES_DSN",
                "postgresql://postgres:jnpa_pw@postgres:5432/postgres",
            ),
            minio_endpoint=os.environ.get("MINIO_ENDPOINT", "minio:9000"),
            minio_access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            minio_model_bucket=os.environ.get("ANOMALY_MODEL_BUCKET", "models"),
            minio_evidence_bucket=os.environ.get("ANOMALY_EVIDENCE_BUCKET", "evidence"),
            minio_prefix=os.environ.get("ANOMALY_MODEL_PREFIX", "anomaly/"),
            minio_secure=_as_bool(os.environ.get("MINIO_SECURE"), False),
            evidence_url_base=os.environ.get("ANOMALY_EVIDENCE_URL_BASE", ""),
            weights_dir=os.environ.get("ANOMALY_WEIGHTS_DIR", str(_HERE / "artifacts")),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


# Corridor + gate cameras whose frame streams the detector consumes by default
# (mirrors the core.camera seed in infra/postgres/init.sql).
DEFAULT_CAMERAS = (
    "CAM-COR-01", "CAM-COR-02", "CAM-COR-03",
    "CAM-COR-04", "CAM-COR-05", "CAM-COR-06",
    "CAM-NSICT-ENT", "CAM-NSICT-EXT",
)
