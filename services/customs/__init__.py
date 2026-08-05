"""Customs module — the real Indian-Customs / ICEGATE document layer for JNPT.

Sourced ONLY from official JNPA customer files (module 5), never synthetic:
  IGM  (CHPOI03 XML), OOC (CHPOI10 XML), SMTP (CHPOI13 XML),
  RMS  (scanning-list .txt), Shipping Bill (.xlsx), LEO (.xlsx).

Layering mirrors services.cargo / services.cfs_ecy:
  parsers/*   — pure file -> typed-dict decoders (no I/O, no DB; deterministic + unit-testable)
  repository  — the only SQL speaker for the jnpa.customs_* tables (bulk, idempotent)
  service     — import orchestration, validation, event emission, workflow transitions

Schema: infra/postgres/migrations/0031_customs.sql
        (bootstrapped at gateway boot by gateway.customs_ext.ensure_customs_schema).
"""
from __future__ import annotations

from .repository import CustomsRepository
from .service import CustomsService, UnknownCustomsFormat, detect_parser

__all__ = ["CustomsService", "CustomsRepository", "detect_parser", "UnknownCustomsFormat"]
