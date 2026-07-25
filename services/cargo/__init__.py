"""Cargo service package — raw-SQL repository + orchestration for core.cargo.

POC-3 is the single common backend for the Traffic Twin (POC-3) and the Cargo
Twin (POC-2): the Cargo CRUD surface (gateway/routers/cargo.py) drives this
service, which is the only writer/reader of the shared ``core.cargo`` table.

Layering mirrors :mod:`services.fastag`:

* :class:`CargoRepository` — the ONLY place that speaks SQL (raw ``text()`` over
  the shared async engine). No ORM.
* :class:`CargoService`    — orchestration + observability + typed errors.
"""

from .repository import (
    CargoConflict,
    CargoNotFound,
    CargoRepository,
    CargoTransitionError,
)
from .service import (
    EVENT_CREATED,
    EVENT_CUSTOMS_STATUS_CHANGED,
    EVENT_DELETED,
    EVENT_GATE_IN,
    EVENT_GATE_MOVEMENT,
    EVENT_GATE_OUT,
    EVENT_LIFECYCLE_CHANGED,
    EVENT_PENDENCY_CREATED,
    EVENT_QUEUE_UPDATED,
    EVENT_RAKE_ASSIGNED,
    EVENT_REEFER_PLANNED,
    EVENT_RELEASED,
    EVENT_STATUS_CHANGED,
    EVENT_UPDATED,
    EVENT_VERIFIED,
    EVENT_VESSEL_DISCHARGED,
    EVENT_YARD_ASSIGNED,
    EVENT_YARD_POSITION_ALLOCATED,
    LC_CREATED,
    LC_RAKE_ASSIGNED,
    LC_REEFER_PLANNED,
    LC_RELEASED,
    LC_SCAN_PENDING,
    LC_VERIFIED,
    LC_VESSEL_DISCHARGED,
    LC_YARD_ASSIGNED,
    LC_YARD_POSITION_ALLOCATED,
    WORKFLOW_TRANSITIONS,
    CargoService,
    allowed_predecessors,
    can_transition,
    scope_filters_for_role,
)

__all__ = [
    "CargoRepository",
    "CargoService",
    "CargoConflict",
    "CargoNotFound",
    "CargoTransitionError",
    "scope_filters_for_role",
    "can_transition",
    "allowed_predecessors",
    "WORKFLOW_TRANSITIONS",
    "EVENT_CREATED",
    "EVENT_RELEASED",
    "EVENT_YARD_ASSIGNED",
    "EVENT_STATUS_CHANGED",
    "EVENT_GATE_MOVEMENT",
    "EVENT_UPDATED",
    "EVENT_DELETED",
    "EVENT_CUSTOMS_STATUS_CHANGED",
    "EVENT_GATE_IN",
    "EVENT_GATE_OUT",
    "EVENT_PENDENCY_CREATED",
    "EVENT_QUEUE_UPDATED",
    "EVENT_LIFECYCLE_CHANGED",
    "EVENT_VESSEL_DISCHARGED",
    "EVENT_YARD_POSITION_ALLOCATED",
    "EVENT_REEFER_PLANNED",
    "EVENT_RAKE_ASSIGNED",
    "EVENT_VERIFIED",
    "LC_CREATED",
    "LC_VESSEL_DISCHARGED",
    "LC_YARD_ASSIGNED",
    "LC_YARD_POSITION_ALLOCATED",
    "LC_REEFER_PLANNED",
    "LC_RAKE_ASSIGNED",
    "LC_SCAN_PENDING",
    "LC_VERIFIED",
    "LC_RELEASED",
]
