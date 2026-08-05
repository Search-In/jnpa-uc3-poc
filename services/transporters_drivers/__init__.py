"""Transporters & Drivers Data-Upload service package — raw-SQL repository + upload
orchestration for the reusable Data-Upload sub-module (migration 0035).

Additive: it owns ONLY the import-ledger tables (core.td_import_file /
td_import_errors) and UPSERTS valid records into the EXISTING masters
(core.transporter on source_company_id, core.driver on licence_no_norm) — no
duplicate business tables. Layering mirrors :mod:`services.cfs_ecy`:

* :class:`TransportersDriversRepository`      — the ONLY place that speaks SQL (raw
  ``text()`` over the shared async engine). No ORM.
* :class:`TransportersDriversUploadService`   — validate -> preview -> confirm-import.
"""

from .repository import TransportersDriversRepository
from .upload_service import TransportersDriversUploadService

__all__ = ["TransportersDriversRepository", "TransportersDriversUploadService"]
