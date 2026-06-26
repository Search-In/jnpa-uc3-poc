"""Service configuration for the identity (face-recognition) verifier.

Reads from the process environment (compose / .env.local), falling back to PoC
defaults so the service runs out of the box. Mirrors ``ingest/vahan_sim/config``.
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
class IdentityConfig:
    # --- Gallery (deterministic synthetic enrolled drivers) ---
    gallery_size: int = 50

    # --- Match thresholds ---
    # score >= verify_threshold            -> VERIFIED
    # provisional_threshold <= score < verify_threshold, or unknown driver
    #                                      -> PROVISIONAL (admit-on-trust)
    # score < provisional_threshold        -> REJECTED
    verify_threshold: float = 0.9
    provisional_threshold: float = 0.5

    # --- Provisional cure window (hours), mirrors the Vahan PROVISIONAL path ---
    cure_window_h: int = 24

    # --- Embedding provider (pluggable; decision logic is unchanged either way) ---
    # "synthetic" (default) keeps the deterministic offline behaviour for the demo
    # and tests; "onnx" runs a real ArcFace CNN over the captured frame when a
    # model file is supplied (IDENTITY_ARCFACE_MODEL). An ONNX failure degrades
    # back to synthetic so the service never hard-fails.
    embedder: str = "synthetic"
    arcface_model_path: str = ""

    # --- Service identity (for jnpa.services registry parity) ---
    service_name: str = "identity"
    service_kind: str = "sim"
    base_url: str = "http://identity:8360"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8360

    # --- Observability ---
    # Prometheus /metrics is mounted on the app itself (port above), so there
    # is no separate metrics port — the scrape target is `identity:8360`.
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "IdentityConfig":
        # Touch shared settings so .env.local is honoured (parity with vahan_sim).
        get_settings()
        return cls(
            gallery_size=_as_int(os.environ.get("IDENTITY_GALLERY_SIZE"), 50),
            verify_threshold=_as_float(os.environ.get("IDENTITY_VERIFY_THRESHOLD"), 0.9),
            provisional_threshold=_as_float(os.environ.get("IDENTITY_PROVISIONAL_THRESHOLD"), 0.5),
            cure_window_h=_as_int(os.environ.get("IDENTITY_CURE_WINDOW_H"), 24),
            embedder=os.environ.get("IDENTITY_EMBEDDER", "synthetic").strip().lower(),
            arcface_model_path=os.environ.get("IDENTITY_ARCFACE_MODEL", ""),
            service_name=os.environ.get("IDENTITY_SERVICE_NAME", "identity"),
            service_kind=os.environ.get("IDENTITY_SERVICE_KIND", "sim"),
            base_url=os.environ.get("IDENTITY_BASE_URL", "http://identity:8360"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8360),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
