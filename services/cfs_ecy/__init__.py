"""CFS-ECY CODECO service package — raw-SQL repository + read orchestration.

Module 13 (UC-III). The single common backend for the off-dock container
gate-movement feeds (CFS-CODECO / ECY-CODECO). Additive + read-only wrt every
existing table; it owns only core.cfs_ecy_movement (+ the v_cfs_ecy_dwell view).

Layering mirrors :mod:`services.cargo` / :mod:`services.driver_master`:

* :class:`CfsEcyRepository` — the ONLY place that speaks SQL (raw ``text()`` over
  the shared async engine). No ORM.
* :class:`CfsEcyService`    — read orchestration + observability.

UC3-003 adds a second, parallel pair for the empty-container TRT KPI, which is
sourced from ``core.container_event`` (the CODECO gate log) rather than from
``core.cfs_ecy_movement``: :class:`EmptyTrtRepository` / :class:`EmptyTrtService`.
"""

from .chain_repository import EcyCfsChainRepository
from .chain_service import EcyCfsChainService
from .repository import CfsEcyRepository
from .service import CfsEcyService
from .trt_repository import EmptyTrtRepository
from .trt_service import EmptyTrtService
from .upload_service import CfsEcyUploadService

__all__ = ["CfsEcyRepository", "CfsEcyService", "CfsEcyUploadService",
           "EcyCfsChainRepository", "EcyCfsChainService",
           "EmptyTrtRepository", "EmptyTrtService"]
