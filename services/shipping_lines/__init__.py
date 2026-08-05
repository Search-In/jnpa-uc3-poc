"""Shipping Lines module — the real Import/Export Advance List (IAL/EAL) and
Electronic Delivery Order (EDO/CODECO) layer for JNPT.

Sourced ONLY from official JNPA customer files (module 4: Shipping Lines), never
synthetic. Layering mirrors services.customs / services.cfs_ecy:
  parsers/*   — pure file -> typed-dict decoders (no I/O, no DB; deterministic + unit-testable)
  repository  — the only SQL speaker for the jnpa.sl_* / core.ref_shipping_line tables
  service     — import orchestration, format detection, event emission, reads

Schema: infra/postgres/migrations/0032_shipping_lines.sql
        (bootstrapped at gateway boot by gateway.shipping_lines_ext.ensure_shipping_lines_schema).
"""
from __future__ import annotations

from .repository import ShippingLinesRepository
from .service import ShippingLinesService, UnknownShippingLineFormat, detect_format
from .upload_service import ShippingLinesUploadService

__all__ = [
    "ShippingLinesService",
    "ShippingLinesRepository",
    "ShippingLinesUploadService",
    "detect_format",
    "UnknownShippingLineFormat",
]
