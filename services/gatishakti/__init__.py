"""GatiShakti reference data over the granted ULIP GATISHAKTI/01..04 APIs.

Layering mirrors :mod:`services.logistics` exactly:
  service.py    — the LIVE -> CACHED -> DATABASE -> FALLBACK chain, refresh
                  orchestration and the health posture
  repository.py — raw-SQL UPSERTs/reads over the core.gs_* tables
                  (migration 0134)

Transport is the shared :class:`integrations.ulip.UlipClient`.

What each API feeds, all backend-only (no screen consumes these directly):

    GATISHAKTI/04  NHAI toll plazas by state -> core.gs_toll_plaza, which is
                   what /api/fastag/toll-enroute resolves against (ULIP grants
                   no route-planning API) and what gives FASTAG/01's free-text
                   `tollPlazaName` a canonical geocode.
    GATISHAKTI/03  named road points (lat/lon) -> core.gs_road_point, the
                   corridor geometry layer.
    GATISHAKTI/01  NH-number road detail  -> core.gs_road_segment
    GATISHAKTI/02  state road network     -> core.gs_road_segment

Note GATISHAKTI/05 (national corridors) is documented by NLDSL but NOT granted
to this account, so nothing here consumes it.
"""
from __future__ import annotations

from .repository import GatiShaktiRepository
from .service import (
    PATH_CACHED,
    PATH_DATABASE,
    PATH_FALLBACK,
    PATH_LIVE,
    STATE_MAHARASHTRA,
    GatiShaktiService,
)

__all__ = [
    "GatiShaktiService",
    "GatiShaktiRepository",
    "STATE_MAHARASHTRA",
    "PATH_LIVE",
    "PATH_CACHED",
    "PATH_DATABASE",
    "PATH_FALLBACK",
]
