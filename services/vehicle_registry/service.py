"""Vehicle registry orchestration (UC3-004).

Thin over the repository: shapes the envelope and attaches the provenance
contract the UI renders. A SYNTHETIC mapping always leaves here carrying its
assumption reference and the seed that generated it, so it can never be
displayed as though it were evidenced.
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from .repository import SYNTHETIC_SEED, VehicleRegistryRepository

log = get_logger("services.vehicle_registry.service")

#: Rendered by the UI beside every synthetic mapping.
ASSUMPTION_TEXT = (
    "TransporterDetails.xlsx and PDP Details.xlsx carry no vehicle numbers, so the "
    "vehicle-to-transporter relationship cannot be loaded from the customer's master "
    "data. Mappings are DOCUMENT_EVIDENCED only where a REAL gate document prints the "
    "transporter; every other mapping is generated and labelled SYNTHETIC under A-G6."
)


def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
    synthetic = row.get("provenance") == "SYNTHETIC"
    return {
        **row,
        "is_synthetic": synthetic,
        "seed": SYNTHETIC_SEED if synthetic else None,
        "assumption_text": ASSUMPTION_TEXT if synthetic else None,
    }


class VehicleRegistryService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VehicleRegistryRepository] = None) -> None:
        self._repo = repository or VehicleRegistryRepository(dsn=dsn)

    async def list_mappings(self, *, provenance: Optional[str] = None,
                            q: Optional[str] = None, limit: int = 50,
                            offset: int = 0) -> Dict[str, Any]:
        t0 = perf_counter()
        rows = await self._repo.list_mappings(provenance=provenance, q=q,
                                              limit=limit, offset=offset)
        total = await self._repo.count_mappings(provenance=provenance, q=q)
        log.info("vehicle_registry.list", extra={"total": total, "returned": len(rows),
                 "ms": round((perf_counter() - t0) * 1000, 1)})
        return {"items": [_shape(r) for r in rows], "total": total,
                "limit": limit, "offset": offset, "count": len(rows)}

    async def by_vehicle(self, plate: str) -> Optional[Dict[str, Any]]:
        row = await self._repo.by_vehicle(plate)
        return _shape(row) if row else None

    async def summary(self) -> Dict[str, Any]:
        counts = {r["provenance"]: r["mappings"] for r in await self._repo.provenance_summary()}
        return {
            "total": sum(counts.values()),
            "document_evidenced": counts.get("DOCUMENT_EVIDENCED", 0),
            "synthetic": counts.get("SYNTHETIC", 0),
            "assumption_ref": "A-G6",
            "assumption_text": ASSUMPTION_TEXT,
            "seed": SYNTHETIC_SEED,
        }
