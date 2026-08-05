"""Rail consumers for the JNPA Port-Data API — the file-backed rail groups.

The JNPA sync engine lands records for the rail groups ``rail-fois`` and
``rail-form11-icd`` as ``routed_status='UNROUTED'`` until a consumer exists.
This package is that consumer (Phase 4): pure parsers + idempotent, sha256-
deduped upload services over the migration-0119 tables, wired into
:mod:`services.jnpa_sync.routing` so ``replay_unrouted`` flips those records to
SUCCESS.

Layering (mirrors services/cfs_ecy):
  parsers/       — pure (bytes, filename) -> ParseResult; no DB, no HTTP
  repository.py  — RailRepository DAO + ensure_rail_schema() boot DDL
  fois_service.py       — RailFoisService  (rail-fois: FOIS Train Intimation CSV)
  form11_icd_service.py — Form11IcdService (rail-form11-icd: Form 11 + CTO;
                          ICD PDFs → REJECTED/UNSUPPORTED_FORMAT)
"""
from __future__ import annotations

from .fois_service import RailFoisService
from .form11_icd_service import Form11IcdService
from .repository import RailRepository, ensure_rail_schema

__all__ = [
    "RailFoisService",
    "Form11IcdService",
    "RailRepository",
    "ensure_rail_schema",
]
