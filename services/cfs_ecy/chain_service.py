"""ECY→CFS chain service — the F-Y1 repositioning lifecycle (UC-III).

Thin over :class:`EcyCfsChainRepository`, mirroring the other services in this
package: it owns observability and the read shaping, while the repository owns
all SQL. The chain is materialised into core.ecy_cfs_chain by ``rebuild()`` and
read back per container or as a filtered list (including anomalies only).
"""
from __future__ import annotations

from time import perf_counter
from typing import Any, Dict, Optional

from jnpa_shared.logging import get_logger

from .chain_repository import EcyCfsChainRepository

log = get_logger("services.cfs_ecy.chain_service")

# Human-readable meaning of each anomaly code, surfaced with the API response so
# the control room does not need the source to interpret a flag.
ANOMALY_LABELS: Dict[str, str] = {
    "DUPLICATE_IN": "more than one CFS gate-IN recorded for this container",
    "MULTI_OUT": "more than one CFS gate-OUT recorded (dwell uses the latest)",
    "OUT_BEFORE_IN": "a CFS gate-OUT precedes the first CFS gate-IN",
    "ORPHAN_CFS_IN": "CFS activity with no preceding ECY gate-OUT",
    "NO_CFS_IN": "an ECY gate-OUT that never arrived at a CFS",
    "LONG_TRANSIT": "road leg longer than 24 h between ECY-out and CFS-in",
}


class EcyCfsChainService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[EcyCfsChainRepository] = None) -> None:
        self._repo = repository or EcyCfsChainRepository(dsn=dsn)

    async def rebuild(self) -> Dict[str, Any]:
        t0 = perf_counter()
        res = await self._repo.rebuild()
        res["ms"] = round((perf_counter() - t0) * 1000, 1)
        return res

    async def list_chains(self, filters, *, limit: int, offset: int) -> Dict[str, Any]:
        rows = await self._repo.list_chains(filters=filters, limit=limit, offset=offset)
        total = await self._repo.count_chains(filters=filters)
        for r in rows:
            r["anomaly_labels"] = [ANOMALY_LABELS.get(c, c) for c in (r.get("anomaly_codes") or [])]
        log.info("ecy_cfs_chain.list", extra={"total": total, "returned": len(rows)})
        return {"items": rows, "total": total, "limit": limit, "offset": offset,
                "count": len(rows)}

    async def get_chain(self, container_number: str) -> Optional[Dict[str, Any]]:
        row = await self._repo.get_chain(container_number)
        if row is None:
            return None
        row["anomaly_labels"] = [ANOMALY_LABELS.get(c, c) for c in (row.get("anomaly_codes") or [])]
        # Name the legs explicitly so the UI renders a chain, not a flat list.
        row["legs"] = [
            {"seq": 1, "leg": "ECY_GATE_OUT", "label": "ECY gate-out (empty released)",
             "ts": row.get("ecy_out_ts"), "present": row.get("ecy_out_ts") is not None},
            {"seq": 2, "leg": "ROAD_MOVEMENT", "label": "road shuttle to CFS",
             "ts": None, "duration_hours": row.get("transit_hours"),
             "present": row.get("transit_hours") is not None},
            {"seq": 3, "leg": "CFS_GATE_IN", "label": "CFS gate-in",
             "ts": row.get("cfs_in_ts"), "present": row.get("cfs_in_ts") is not None},
            {"seq": 4, "leg": "CFS_DWELL", "label": "stuffing / dwell at CFS",
             "ts": None, "duration_hours": row.get("dwell_hours"),
             "present": row.get("dwell_hours") is not None},
            {"seq": 5, "leg": "CFS_GATE_OUT", "label": "CFS gate-out (to terminal for export)",
             "ts": row.get("cfs_out_ts"), "present": row.get("cfs_out_ts") is not None},
            # Terminal export gate-in is NOT IN THE CORPUS — reported honestly
            # rather than fabricated (the client document marks it absent).
            {"seq": 6, "leg": "TERMINAL_EXPORT_GATE_IN",
             "label": "terminal gate-in for export", "ts": None, "present": False,
             "note": "not in corpus — no source file records this leg"},
        ]
        return row

    async def stats(self) -> Dict[str, Any]:
        res = await self._repo.stats()
        res["anomaly_labels"] = ANOMALY_LABELS
        return res
