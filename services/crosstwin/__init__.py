"""UC-II <-> UC-III cross-twin durability (migration 0115).

The XT-2 contract (``jnpa.crosstwin.deferred-arrival``) was consumed correctly but
applied to an in-memory slot book only, so a consumed UC-II window disappeared on
gateway restart and could never be shown again. This package persists every
applied window to ``core.deferred_arrival_window`` and replays them on boot, so
the TAS metering state is durable and the proof surface survives a refresh.
"""
from .repository import DeferredArrivalRepository

__all__ = ["DeferredArrivalRepository"]
