"""COARRI / COPRAR vessel-side container-move consumer (edi-messages group).

The JNPA Port-Data API's edi-messages group delivers, besides the CODECO gate
moves, per-vessel-call container documents:

  COARRI  ``<ContLoadingNDischargeOder>``  loading/discharge REPORT
  COPRAR  ``<AdvContainerList>``           advance container list (ORDER)

This package parses those XML documents into ``core.edi_vessel_container``
(one row per container per document) behind the ``core.edi_import_file``
ledger — migration 0123, mirroring the rail (0119) consumer pattern:
idempotent, duplicate-safe, provenance-tagged.
"""
from .service import EdiVesselService
from .repository import EdiVesselRepository, ensure_edi_vessel_schema

__all__ = ["EdiVesselService", "EdiVesselRepository",
           "ensure_edi_vessel_schema"]
