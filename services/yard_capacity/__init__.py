"""UC-3 peak yard utilisation + truck arrival management (additive module).

    model.py       pure decisions (thresholds, congestion pressure, hold plan)
    repository.py  core.yard_capacity_state / _event / core.truck_arrival_hold
    service.py     orchestration over injected ports (arrivals, parking, alerts,
                   driver push, WS) — never imports the gateway
"""
from .model import (ArrivalCandidate, HoldPlan, HOLD_ACTIVE, HOLD_CANCELLED,
                    HOLD_RELEASED, HOLD_REASON, STATUS_CRITICAL, STATUS_ELEVATED,
                    STATUS_HIGH, STATUS_NORMAL)
from .repository import YardCapacityRepository
from .service import (ADVISORY_HOLD, ADVISORY_RELEASE, WS_ARRIVAL, WS_YARD,
                      YardCapacityService, YardThresholds)

__all__ = [
    "ArrivalCandidate", "HoldPlan", "HOLD_ACTIVE", "HOLD_CANCELLED",
    "HOLD_RELEASED", "HOLD_REASON", "STATUS_CRITICAL", "STATUS_ELEVATED",
    "STATUS_HIGH", "STATUS_NORMAL", "YardCapacityRepository",
    "YardCapacityService", "YardThresholds",
    "ADVISORY_HOLD", "ADVISORY_RELEASE", "WS_ARRIVAL", "WS_YARD",
]
