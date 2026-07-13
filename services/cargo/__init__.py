"""Cargo service package — raw-SQL repository + orchestration for jnpa.cargo.

POC-3 is the single common backend for the Traffic Twin (POC-3) and the Cargo
Twin (POC-2): the Cargo CRUD surface (gateway/routers/cargo.py) drives this
service, which is the only writer/reader of the shared ``jnpa.cargo`` table.

Layering mirrors :mod:`services.fastag`:

* :class:`CargoRepository` — the ONLY place that speaks SQL (raw ``text()`` over
  the shared async engine). No ORM.
* :class:`CargoService`    — orchestration + observability + typed errors.
"""

from .repository import CargoConflict, CargoNotFound, CargoRepository
from .service import (
    EVENT_CREATED,
    EVENT_DELETED,
    EVENT_GATE_MOVEMENT,
    EVENT_RELEASED,
    EVENT_STATUS_CHANGED,
    EVENT_UPDATED,
    EVENT_YARD_ASSIGNED,
    CargoService,
    scope_filters_for_role,
)

__all__ = [
    "CargoRepository",
    "CargoService",
    "CargoConflict",
    "CargoNotFound",
    "scope_filters_for_role",
    "EVENT_CREATED",
    "EVENT_RELEASED",
    "EVENT_YARD_ASSIGNED",
    "EVENT_STATUS_CHANGED",
    "EVENT_GATE_MOVEMENT",
    "EVENT_UPDATED",
    "EVENT_DELETED",
]
