"""Transporters & Drivers Data-Upload service package — raw-SQL repository + upload
orchestration for the reusable Data-Upload sub-module (migration 0035).

Additive: it owns ONLY the import-ledger tables (jnpa.td_import_files /
td_import_errors) and UPSERTS valid records into the EXISTING masters
(jnpa.transporters on source_company_id, jnpa.driver_master on licence_no_norm) — no
duplicate business tables. Layering mirrors :mod:`services.cfs_ecy`:

* :class:`TransportersDriversRepository`      — the ONLY place that speaks SQL (raw
  ``text()`` over the shared async engine). No ORM.
* :class:`TransportersDriversUploadService`   — validate -> preview -> confirm-import.
"""

from .repository import TransportersDriversRepository
from .upload_service import TransportersDriversUploadService

__all__ = ["TransportersDriversRepository", "TransportersDriversUploadService"]
