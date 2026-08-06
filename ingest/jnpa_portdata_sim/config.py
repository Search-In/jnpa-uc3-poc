"""Configuration for the JNPA Port-Data API simulator.

Reads from the process environment (compose / .env.local), falling back to
PoC defaults so the service runs out of the box.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _as_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_float(value: str | None, default: float) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class SimConfig:
    # --- Dataset: the sample-data-pack Data/ folder to index ---
    # In compose the host dump is mounted here; tests point it at the real
    # local dump. Empty/missing dir => the sim serves an empty catalogue.
    data_dir: str = "data/jnpa_dump"

    # --- Auth: emails whose derived client keys are accepted (the keygen
    # algorithm: base64(sha256(lower(email)).hexdigest()[:20])), plus any
    # literal extra keys (comma-separated) for tests ---
    client_emails: List[str] = field(
        default_factory=lambda: ["sim@keltron.test"])
    extra_keys: List[str] = field(default_factory=list)
    token_ttl_s: float = 3600.0

    # --- Faithful-defect emulation (docs/JNPA_API_DEFECTS.md) ---
    faithful: bool = True            # sequential ids, cursor=fileRef, 5-field
                                     # report envelope, no requestId, header
                                     # omissions, slow bad-key 401
    report_items: str = "empty"      # empty | synthetic (exercise mappers)
    force_429: int = 0               # answer 429 (no Retry-After) to the next
                                     # N data requests — set via env or
                                     # POST /admin/force-429
    bad_key_delay_s: float = 0.25    # the deliberate bad-key slowdown

    # --- HTTP ---
    host: str = "0.0.0.0"
    port: int = 8500

    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "SimConfig":
        emails = [e.strip() for e in
                  os.environ.get("JNPA_SIM_CLIENT_EMAILS",
                                 "sim@keltron.test").split(",") if e.strip()]
        extra = [k.strip() for k in
                 os.environ.get("JNPA_SIM_EXTRA_KEYS", "").split(",")
                 if k.strip()]
        return cls(
            data_dir=os.environ.get("JNPA_SIM_DATA_DIR", "data/jnpa_dump"),
            client_emails=emails,
            extra_keys=extra,
            token_ttl_s=_as_float(os.environ.get("JNPA_SIM_TOKEN_TTL_S"), 3600.0),
            faithful=_as_bool(os.environ.get("JNPA_SIM_FAITHFUL"), True),
            report_items=os.environ.get("JNPA_SIM_REPORT_ITEMS", "empty"),
            force_429=_as_int(os.environ.get("JNPA_SIM_FORCE_429"), 0),
            bad_key_delay_s=_as_float(os.environ.get("JNPA_SIM_BAD_KEY_DELAY_S"), 0.25),
            host=os.environ.get("HOST", "0.0.0.0"),
            port=_as_int(os.environ.get("PORT"), 8500),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
        )
