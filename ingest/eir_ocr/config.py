"""Service configuration for the EIR OCR ingest service."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


@dataclass
class EirOcrConfig:
    host: str = "0.0.0.0"
    port: int = 8210
    log_level: str = "INFO"
    cache_size: int = 64
    # Early-exit when these high-value fields are already extracted.
    early_exit_fields: tuple[str, ...] = ("ContainerNo", "LICNo", "EIRNo")
    early_exit_min_hits: int = 2
    # Optional default images dir for batch verify (workspace EIR/).
    images_dir: str = ""
    binarize_threshold: int = 140

    @classmethod
    def from_env(cls) -> "EirOcrConfig":
        return cls(
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT") or os.environ.get("EIR_OCR_PORT"), 8210),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            cache_size=_as_int(os.environ.get("EIR_OCR_CACHE_SIZE"), 64),
            early_exit_min_hits=_as_int(os.environ.get("EIR_OCR_EARLY_EXIT_MIN"), 2),
            images_dir=os.environ.get("EIR_IMAGES_DIR", ""),
            binarize_threshold=_as_int(os.environ.get("EIR_OCR_BINARIZE_THRESHOLD"), 140),
        )
