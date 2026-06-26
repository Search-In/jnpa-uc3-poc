"""Runtime mode gating for the identity / driver-enrolment subsystem.

Two modes, driven by ``APP_ENV`` (reusing the same dev/prod classification as the
auth layer so there is ONE source of truth):

  * DEV / LOCAL  (APP_ENV=development|dev|local|test, the default) — every
    resilience fallback is allowed: in-memory enrolment store, synthetic identity,
    base64 image storage. The demo and tests run with zero infra.

  * PRODUCTION   (any other APP_ENV) — fallbacks are DISABLED so behaviour is
    deterministic and secure: Postgres is REQUIRED (no in-memory), MinIO is
    REQUIRED (no base64), ArcFace ONNX is REQUIRED (no synthetic pass on a real
    capture), and auth is REQUIRED (enforced at startup by validate_auth_config).

The helpers here are the single place the routes/services ask "may I fall back?".
"""
from __future__ import annotations

import os

from .auth import app_env, is_production_like


def _env_allow_fallback() -> bool:
    """ALLOW_FALLBACK env (default true). Lets a dev box opt INTO strict mode for
    testing. Production ignores this — fallbacks are always off there."""
    return os.environ.get("ALLOW_FALLBACK", "true").strip().lower() in {"1", "true", "yes", "on"}


def _fallbacks_allowed() -> bool:
    return (not is_production_like()) and _env_allow_fallback()


class ProductionSafetyError(RuntimeError):
    """A required production dependency (DB / object store / model) is unavailable.

    Routes translate this into a structured 503 instead of silently degrading —
    in production we fail loud and safe rather than serve a fallback result.
    """

    def __init__(self, component: str, detail: str = "") -> None:
        self.component = component
        self.detail = detail
        super().__init__(f"{component} required in production but unavailable: {detail}")


def production_mode() -> bool:
    """True when the deployment must run strict (no fallbacks)."""
    return is_production_like()


def allow_memory_store() -> bool:
    """In-memory enrolment store is dev-only (and disabled if ALLOW_FALLBACK=false)."""
    return _fallbacks_allowed()


def allow_synthetic_identity() -> bool:
    """Synthetic / deterministic identity matching is dev-only."""
    return _fallbacks_allowed()


def allow_base64_image_fallback() -> bool:
    """Keeping a face frame as base64 (instead of MinIO) is dev-only."""
    return _fallbacks_allowed()


def mode_name() -> str:
    return "production" if production_mode() else f"development ({app_env()})"


__all__ = [
    "ProductionSafetyError",
    "production_mode",
    "allow_memory_store",
    "allow_synthetic_identity",
    "allow_base64_image_fallback",
    "mode_name",
]
