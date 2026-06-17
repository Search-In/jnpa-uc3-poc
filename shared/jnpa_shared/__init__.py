"""jnpa_shared — shared config, schemas, and IO helpers for the JNPA UC-III PoC."""

__version__ = "0.1.0"

from .config import Settings, get_settings  # noqa: E402,F401
from . import cloudevents  # noqa: E402,F401

__all__ = ["Settings", "get_settings", "cloudevents", "__version__"]
