"""Data Quality ledger service package — read-only over ``core.dq_issue``.

``core.dq_issue`` has been written by every corpus importer in this repository
since the first ingest, but nothing ever read it back: the findings existed only
in the database and in each importer's console output. UC3-003 needs the ECY
pairing anomaly to be *demonstrably visible*, so this package exposes the ledger
that was already there — it defines no new storage and writes nothing.

Layering mirrors :mod:`services.cfs_ecy`:

* :class:`DqRepository` — the ONLY place that speaks SQL (raw ``text()`` over the
  shared async engine).
* :class:`DqService`    — read orchestration + observability.
"""

from .repository import DqRepository
from .service import DqService

__all__ = ["DqRepository", "DqService"]
