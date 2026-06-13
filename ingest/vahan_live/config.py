"""Service configuration for the live Surepass-backed Vahan adapter."""
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


# Surepass KYC API endpoints (per the bid's integration plan).
SUREPASS_BASE = "https://kyc-api.surepass.io/api/v1"
SUREPASS_RC = f"{SUREPASS_BASE}/rc/rc-full"
SUREPASS_DL = f"{SUREPASS_BASE}/driving-license/driving-license"
SUREPASS_FASTAG = f"{SUREPASS_BASE}/fastag/fastag-search"


@dataclass
class LiveConfig:
    surepass_api_token: str = ""
    surepass_rc_url: str = SUREPASS_RC
    surepass_dl_url: str = SUREPASS_DL
    surepass_fastag_url: str = SUREPASS_FASTAG
    upstream_timeout_s: float = 8.0

    # --- Service identity (jnpa.services registry) ---
    service_name: str = "vahan"
    service_kind: str = "live"
    base_url: str = "http://vahan-live:8202"

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8202

    # --- DB ---
    postgres_dsn: str = ""

    log_level: str = "INFO"

    @property
    def enabled(self) -> bool:
        """Live path is enabled only when a non-empty Surepass token is set."""
        return bool(self.surepass_api_token and self.surepass_api_token.strip())

    @classmethod
    def from_env(cls) -> "LiveConfig":
        shared = get_settings()
        return cls(
            surepass_api_token=os.environ.get("SUREPASS_API_TOKEN", shared.surepass_api_token),
            surepass_rc_url=os.environ.get("SUREPASS_RC_URL", SUREPASS_RC),
            surepass_dl_url=os.environ.get("SUREPASS_DL_URL", SUREPASS_DL),
            surepass_fastag_url=os.environ.get("SUREPASS_FASTAG_URL", SUREPASS_FASTAG),
            upstream_timeout_s=_as_float(os.environ.get("SUREPASS_TIMEOUT_S"), 8.0),
            service_name=os.environ.get("VAHAN_SERVICE_NAME", "vahan"),
            service_kind=os.environ.get("VAHAN_SERVICE_KIND", "live"),
            base_url=os.environ.get("VAHAN_BASE_URL", "http://vahan-live:8202"),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8202),
            postgres_dsn=os.environ.get("POSTGRES_DSN", shared.postgres_dsn),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
