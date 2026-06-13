"""Structured JSON logging via structlog.

`configure_logging()` installs a JSON renderer and binds a `trace_id` (taken
from the TRACE_ID env var, falling back to the configured default). Call it
once at service start, then use `get_logger(__name__)`.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog


def _trace_id() -> str:
    return os.environ.get("TRACE_ID", "local-dev")


def configure_logging(level: str = "INFO") -> None:
    """Configure stdlib logging + structlog to emit one JSON object per line."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bind a process-wide trace id so every line is correlatable.
    structlog.contextvars.bind_contextvars(trace_id=_trace_id())


def get_logger(name: str | None = None, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger, optionally with initial context."""
    logger = structlog.get_logger(name)
    if initial:
        logger = logger.bind(**initial)
    return logger


__all__ = ["configure_logging", "get_logger"]
