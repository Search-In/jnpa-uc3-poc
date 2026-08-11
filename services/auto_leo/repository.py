"""Auto-LEO four-way join persistence (UC3-040).

Reads the four evidence streams the export gate captures — e-seal, Form 13,
weighbridge, ICEGATE — out of ``core.gate_capture`` and shapes them into the
record objects the pure reconciler in ``gate-data/leo.py`` consumes. Also reads
the X4 weighbridge-reroute ledger (``core.weighbridge_reroute``, migration 0136).

The join key is the container number. A container that has only three of the four
streams comes back with the fourth as ``None`` — deliberately, so the reconciler
can report MISSING rather than silently treating an absent record as a pass.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.auto_leo.repository")

#: capture_type -> the source name the reconciler uses.
CAPTURE_TO_SOURCE = {
    "ESEAL": "eseal",
    "FORM13": "form13",
    "WEIGHBRIDGE": "weighbridge",
    "ICEGATE": "icegate",
}


def _payload(raw: Any) -> Dict[str, Any]:
    """gate_capture.payload is jsonb — normalise to a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return {}


class AutoLeoRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def captures_for(self, containers: Sequence[str]) -> list[dict]:
        """Every capture row for the given containers, newest first per stream."""
        if not containers:
            return []
        return await self._rows(
            """
            SELECT id, capture_type, container_no, vehicle_plate, gate_id,
                   source_mode, status, captured_at, payload, evidence_uri
              FROM core.gate_capture
             WHERE container_no = ANY(:cns)
               AND capture_type IN ('ESEAL', 'FORM13', 'WEIGHBRIDGE', 'ICEGATE')
             ORDER BY container_no, capture_type, captured_at DESC
            """,
            {"cns": list(containers)},
        )

    async def export_containers(self, *, limit: int = 50,
                                source_mode: Optional[str] = None) -> list[str]:
        """Containers that have at least a Form 13 — the export leg's anchor.

        A container with no Form 13 is not an export truck at the gate, so it has
        no Auto-LEO decision to make and is not put on the board.

        Ordering puts containers whose Form 13 is one of the REAL corpus
        documents FIRST, then the rest by recency. Ordering by recency alone hid
        them: the real slips are dated June 2026 while the generated captures
        carry later timestamps, so all four document-anchored cases — including
        the ticket's own MEDU1777575 / FFAU4770682 / BMOU5841115 examples — fell
        past the default limit and the board opened showing only synthetic rows.
        The cases backed by the customer's own paperwork are the ones worth
        seeing first.
        """
        conds = ["gc.capture_type = 'FORM13'", "gc.container_no IS NOT NULL"]
        params: Dict[str, Any] = {"limit": limit}
        if source_mode:
            conds.append("gc.source_mode = :sm")
            params["sm"] = source_mode
        rows = await self._rows(
            """
            SELECT gc.container_no,
                   max(gc.captured_at) AS latest,
                   bool_or(gd.doc_id IS NOT NULL) AS from_real_document
              FROM core.gate_capture gc
              LEFT JOIN core.gate_document gd
                     ON gd.container_no = gc.container_no
                    AND gd.doc_category = 'FORM13'
                    AND gd.data_origin = 'REAL'
            """
            f" WHERE {' AND '.join(conds)} "
            " GROUP BY gc.container_no"
            " ORDER BY from_real_document DESC, latest DESC"
            " LIMIT :limit",
            params,
        )
        return [r["container_no"] for r in rows]

    async def reroutes_for(self, containers: Sequence[str]) -> list[dict]:
        """X4 weighbridge-reroute records for the given containers."""
        if not containers:
            return []
        return await self._rows(
            """
            SELECT reroute_id, container_no, vehicle_plate, failed_wb_id,
                   alternate_wb_id, reason, customs_notified, notified_at,
                   created_at, simulated
              FROM core.weighbridge_reroute
             WHERE container_no = ANY(:cns)
             ORDER BY created_at DESC
            """,
            {"cns": list(containers)},
        )

    async def real_form13_documents(self) -> list[dict]:
        """The REAL corpus Form 13s (core.gate_document, data_origin='REAL').

        Used to mark a board row as anchored to a document the customer actually
        supplied, rather than to a generated one.
        """
        return await self._rows(
            """
            SELECT doc_id, doc_variant, doc_ref, container_no, vehicle_no,
                   seal1, seal2, gross_weight_kg, terminal_id, doc_ts, data_origin
              FROM core.gate_document
             WHERE doc_category = 'FORM13' AND data_origin = 'REAL'
             ORDER BY doc_variant
            """
        )
