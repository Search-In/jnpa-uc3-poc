"""Performance & Daily Reports service package — raw-SQL repository + read orchestration.

Module 12 (UC-III). The common backend for the official JNPA performance reports
(Daily Status Report, monthly JN Port TEUs, NLDS/LDB Analytics). Additive +
read-only wrt every existing table; it owns only the jnpa.perf_* tables.

Layering mirrors :mod:`services.cfs_ecy` / :mod:`services.driver_master`:

* :class:`PerformanceRepository` — the ONLY place that speaks SQL (raw ``text()``
  over the shared async engine). No ORM.
* :class:`PerformanceService`    — read orchestration + observability.
"""

from .repository import PerformanceRepository
from .service import PerformanceService
from .upload_repository import UploadRepository
from .upload_service import UploadService

__all__ = ["PerformanceRepository", "PerformanceService", "UploadRepository", "UploadService"]
