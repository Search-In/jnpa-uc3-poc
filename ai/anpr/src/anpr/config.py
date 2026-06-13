"""Service configuration for the ANPR + OCR inference service (ai/anpr).

Reads from the process environment (the container gets these from compose /
.env.local). Falls back to PoC defaults so the service runs out of the box on a
CPU-only host with no GPU, no paddle, and no downloaded weights (degraded mode).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_RESOURCES = Path(__file__).resolve().parents[2] / "resources"


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
class AnprAiConfig:
    host: str = "0.0.0.0"
    port: int = 8301

    # Detector
    yolo_weights: str = str(_RESOURCES / "license_plate_detector.pt")
    detect_conf: float = 0.25

    # OCR
    char_dict_path: str = str(_RESOURCES / "indian_plate_chars.txt")
    rec_model_dir: str = str(_RESOURCES / "rec_indian")
    use_gpu: bool = False

    # Eval
    eval_set_size: int = 200          # plates rendered for the held-out slice
    eval_target_pct: float = 95.0     # combined weighted accuracy target

    # MinIO (weights persistence)
    minio_endpoint: str = "minio:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "models"
    minio_secure: bool = False

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "AnprAiConfig":
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8301),
            yolo_weights=os.environ.get("ANPR_YOLO_WEIGHTS", str(_RESOURCES / "license_plate_detector.pt")),
            detect_conf=_as_float(os.environ.get("ANPR_DETECT_CONF"), 0.25),
            char_dict_path=os.environ.get("ANPR_CHAR_DICT", str(_RESOURCES / "indian_plate_chars.txt")),
            rec_model_dir=os.environ.get("ANPR_REC_DIR", str(_RESOURCES / "rec_indian")),
            use_gpu=_as_bool(os.environ.get("ANPR_USE_GPU"), False),
            eval_set_size=_as_int(os.environ.get("ANPR_EVAL_SIZE"), 200),
            eval_target_pct=_as_float(os.environ.get("ANPR_EVAL_TARGET_PCT"), 95.0),
            minio_endpoint=os.environ.get("MINIO_ENDPOINT", "minio:9000"),
            minio_access_key=os.environ.get("MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.environ.get("MINIO_SECRET_KEY", "minioadmin"),
            minio_bucket=os.environ.get("ANPR_MODEL_BUCKET", "models"),
            minio_secure=_as_bool(os.environ.get("MINIO_SECURE"), False),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
