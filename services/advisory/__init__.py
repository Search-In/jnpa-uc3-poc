"""Durable driver-advisory store (migration 0115).

Re-route advisories and their ACKs used to live in ``gateway.routers.trucks``
module dicts, so every advisory vanished on gateway restart and a driver who
refreshed the PWA lost the banner. This package mirrors them into RDS while the
in-memory dict stays as a hot cache, so the read path is unchanged in latency and
the write path simply gains durability.
"""
from .repository import AdvisoryRepository

__all__ = ["AdvisoryRepository"]
