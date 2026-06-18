"""Service configuration for anpr-ingest.

Reads from the process environment (the container gets these from compose /
.env.local). Falls back to sane PoC defaults so the service runs out of the box
with zero clips and no API keys.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

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


# Map a clip filename stem to the JNPA camera id it stands in for. Anything not
# listed falls back to the file stem upper-cased.
CLIP_CAMERA_MAP = {
    "cam_g1_entry": "CAM-NSICT-ENT",
    "cam_g1_exit": "CAM-NSICT-EXT",
    "cam_corridor_km5": "CAM-COR-01",
    "cam_corridor_km30": "CAM-COR-05",
}


@dataclass
class AnprConfig:
    # --- Replay ---
    clips_dir: str = "/data/clips"
    target_fps: float = 5.0           # frames/sec sampled from each clip
    snapshot_interval_s: float = 1.0  # per-second snapshot cadence (spec)
    no_feed_interval_s: float = 5.0   # emit a no_feed health event this often

    # --- Detection ---
    dry_run: bool = True
    yolo_weights: str = "yolov8n.pt"
    detect_conf: float = 0.25
    # When YOLO finds no vehicles in a frame, still emit a degraded full-frame
    # candidate. Keeps synthetic/empty footage flowing through the pipeline for
    # the PoC; set false for production where only real detections should emit.
    emit_on_empty: bool = True
    # AI ANPR + OCR inference service (ai/anpr, Sub-Criterion 2A). The ingest
    # service POSTs each plate crop here as multipart when DRY_RUN=false.
    ai_anpr_url: str = "http://anpr:8301/infer"
    ai_timeout_s: float = 2.0

    # --- Weather / condition ---
    weather_interval_s: float = 600.0  # 10 minutes
    openweather_api_key: str = ""
    port_lat: float = 18.9489
    port_lon: float = 72.9492
    # Offline-first: when true, never call the weather network; the tagger uses
    # the demo override (or "clear"). Set by OFFLINE/DATA_MODE.
    offline: bool = True
    # Presenter override forcing a condition (CLEAR|DUST|FOG|NIGHT|"" for auto).
    # Lets the demo show ≥95% OCR in CLEAR and degradation in FOG/NIGHT on click.
    condition_override: str = ""
    # Global seed (from shared settings) so the synthetic OCR-confidence draw in
    # DRY_RUN replays identically.
    seed: int = 1337

    # --- Kafka ---
    kafka_brokers: str = "kafka:9092"
    topic: str = "anpr.reads"

    # --- Shared frame bus (Redis Streams) ---
    # Mirror sampled jpeg frames onto "frames.{camera_id}" so ai/anomaly (and
    # later ai/anpr) can consume the same feed. Trimmed to the last N entries to
    # bound Redis memory. Disabled (publish_frames=false) -> no bus writes.
    publish_frames: bool = True
    redis_url: str = "redis://redis:6379/0"
    frame_bus_maxlen: int = 600
    frame_jpeg_quality: int = 70

    # --- Observability ---
    metrics_port: int = 9101
    log_level: str = "INFO"

    # vehicle COCO class ids (car, motorcycle, bus, truck) for YOLOv8
    vehicle_class_ids: List[int] = field(default_factory=lambda: [2, 3, 5, 7])

    @classmethod
    def from_env(cls) -> "AnprConfig":
        shared = get_settings()
        return cls(
            clips_dir=os.environ.get("CLIPS_DIR", "/data/clips"),
            target_fps=_as_float(os.environ.get("TARGET_FPS"), 5.0),
            snapshot_interval_s=_as_float(os.environ.get("SNAPSHOT_INTERVAL_S"), 1.0),
            no_feed_interval_s=_as_float(os.environ.get("NO_FEED_INTERVAL_S"), 5.0),
            dry_run=_as_bool(os.environ.get("DRY_RUN"), True),
            yolo_weights=os.environ.get("YOLO_WEIGHTS", "yolov8n.pt"),
            detect_conf=_as_float(os.environ.get("DETECT_CONF"), 0.25),
            emit_on_empty=_as_bool(os.environ.get("EMIT_ON_EMPTY"), True),
            ai_anpr_url=os.environ.get("AI_ANPR_URL", "http://anpr:8301/infer"),
            ai_timeout_s=_as_float(os.environ.get("AI_TIMEOUT_S"), 2.0),
            weather_interval_s=_as_float(os.environ.get("WEATHER_INTERVAL_S"), 600.0),
            openweather_api_key=os.environ.get("OPENWEATHER_API_KEY", shared.openweather_api_key),
            port_lat=_as_float(os.environ.get("PORT_LAT"), shared.port_lat),
            port_lon=_as_float(os.environ.get("PORT_LON"), shared.port_lon),
            offline=_as_bool(os.environ.get("OFFLINE"), shared.is_offline),
            condition_override=os.environ.get("ANPR_CONDITION_OVERRIDE", ""),
            seed=int(os.environ.get("SEED", shared.seed)),
            kafka_brokers=os.environ.get("KAFKA_BROKERS", shared.kafka_brokers),
            topic=os.environ.get("ANPR_TOPIC", "anpr.reads"),
            publish_frames=_as_bool(os.environ.get("PUBLISH_FRAMES"), True),
            redis_url=os.environ.get("REDIS_URL", shared.redis_url),
            frame_bus_maxlen=_as_int(os.environ.get("FRAME_BUS_MAXLEN"), 600),
            frame_jpeg_quality=_as_int(os.environ.get("FRAME_JPEG_QUALITY"), 70),
            metrics_port=_as_int(os.environ.get("METRICS_PORT"), 9101),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )


def camera_id_for_clip(stem: str) -> str:
    return CLIP_CAMERA_MAP.get(stem, stem.upper())
