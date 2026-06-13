"""Re-export of the shared structured logger so gateway modules can use the
short ``from .logging import get_logger`` form. See ``jnpa_shared.logging``."""
from __future__ import annotations

from jnpa_shared.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]
