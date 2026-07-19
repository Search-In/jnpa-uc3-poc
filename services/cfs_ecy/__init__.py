"""CFS-ECY CODECO service package — raw-SQL repository + read orchestration.

Module 13 (UC-III). The single common backend for the off-dock container
gate-movement feeds (CFS-CODECO / ECY-CODECO). Additive + read-only wrt every
existing table; it owns only jnpa.cfs_ecy_movements (+ the v_cfs_ecy_dwell view).

Layering mirrors :mod:`services.cargo` / :mod:`services.driver_master`:

* :class:`CfsEcyRepository` — the ONLY place that speaks SQL (raw ``text()`` over
  the shared async engine). No ORM.
* :class:`CfsEcyService`    — read orchestration + observability.
"""

from .repository import CfsEcyRepository
from .service import CfsEcyService

__all__ = ["CfsEcyRepository", "CfsEcyService"]
