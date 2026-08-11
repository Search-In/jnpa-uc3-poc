"""Trip resolver persistence (UC3-024) and visit timeline sources (UC3-025).

Reads ``core.gate_document`` — the 12 REAL gate documents imported verbatim from
the customer corpus by scripts/import_gate_documents.py. One document is one
visit leg, so a document row is the trip record every search key resolves to.

The five searchable keys all live on that one row, which is why they can resolve
to the SAME trip rather than to four different views of it:

    doc_ref      Form 13 e-gate number / EIR number   (16497850, 4339869)
    container_no ISO 6346 container                    (MEDU1777575)
    vehicle_no   tractor plate                         (MH43CK1959)
    seal1/seal2  line seal / customs (e-)seal          (0008264 / 5826371)
    pin_no       PIN pickup ticket code

Nothing is inferred. A key that matches no document returns no trip.
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.trip_search.repository")

#: Every column a trip is searchable by, and the key kind each represents.
SEARCH_COLUMNS = {
    "doc_ref": "DOCUMENT_NO",
    "pin_no": "PIN",
    "container_no": "CONTAINER",
    "vehicle_no": "PLATE",
    "seal1": "ESEAL",
    "seal2": "ESEAL",
}

_SELECT = """
    SELECT d.doc_id, d.doc_category, d.doc_variant, d.doc_ref, d.pin_no, d.visit_id,
           d.doc_ts, d.container_no, d.iso_code, d.load_status, d.gross_weight_kg,
           d.seal1, d.seal2, d.vehicle_no, d.bat_no, d.driver_name, d.driver_licence,
           d.transporter_name, d.truck_in_ts, d.truck_out_ts, d.gate_no,
           d.yard_position, d.vessel_name, d.voyage, d.pol, d.pod, d.booking_no,
           d.cfs, d.group_code, d.attrs, d.image_file, d.source_file, d.data_origin,
           t.code AS terminal_code, t.name AS terminal_name, t.operator AS terminal_operator
      FROM core.gate_document d
      LEFT JOIN core.ref_terminal t ON t.terminal_id = d.terminal_id
"""


def norm(value: str) -> str:
    """Uppercase, strip every non-alphanumeric — the form keys are compared in.

    Plates are printed with and without spaces/hyphens, and seal numbers are
    printed with leading zeros that a user rarely types. Comparing normalised
    forms is what lets "mh43 ck 1959" find MH43CK1959.
    """
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def parse_attrs(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    return {}


class TripSearchRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def find_by_key(self, q: str) -> list[dict]:
        """Every document whose searchable columns match ``q`` (normalised).

        One statement across all five key columns so a caller does not have to
        say which kind of identifier it holds — that is the whole point of a
        single search box.
        """
        needle = norm(q)
        if not needle:
            return []
        conds = " OR ".join(
            f"regexp_replace(upper(COALESCE(d.{col}, '')), '[^A-Z0-9]', '', 'g') = :needle"
            for col in SEARCH_COLUMNS
        )
        return await self._rows(f"{_SELECT} WHERE {conds} ORDER BY d.doc_ts DESC",
                                {"needle": needle})

    async def find_by_prefix(self, q: str, limit: int = 10) -> list[dict]:
        """Partial-key suggestions when nothing matched exactly."""
        needle = norm(q)
        if len(needle) < 3:
            return []
        conds = " OR ".join(
            f"regexp_replace(upper(COALESCE(d.{col}, '')), '[^A-Z0-9]', '', 'g') LIKE :needle"
            for col in SEARCH_COLUMNS
        )
        return await self._rows(
            f"{_SELECT} WHERE {conds} ORDER BY d.doc_ts DESC LIMIT :limit",
            {"needle": f"%{needle}%", "limit": limit})

    async def by_doc_id(self, doc_id: int) -> Optional[dict]:
        rows = await self._rows(f"{_SELECT} WHERE d.doc_id = :id", {"id": doc_id})
        return rows[0] if rows else None

    async def related_documents(self, *, container_no: Optional[str],
                                vehicle_no: Optional[str]) -> list[dict]:
        """Other documents for the same container or tractor — the visit's paper
        trail across terminals."""
        if not container_no and not vehicle_no:
            return []
        # CAST(... AS text) rather than the ::text shorthand: SQLAlchemy's text()
        # parser reads ":cn::text" as a bind parameter named "cn:" and emits
        # invalid SQL.
        return await self._rows(
            f"""{_SELECT}
             WHERE (CAST(:cn AS text) IS NOT NULL AND d.container_no = :cn)
                OR (CAST(:vn AS text) IS NOT NULL AND
                    regexp_replace(upper(COALESCE(d.vehicle_no, '')), '[^A-Z0-9]', '', 'g')
                    = regexp_replace(upper(CAST(:vn AS text)), '[^A-Z0-9]', '', 'g'))
             ORDER BY d.doc_ts""",
            {"cn": container_no, "vn": vehicle_no})

    async def gate_events_for(self, vehicle_no: Optional[str],
                              limit: int = 50) -> list[dict]:
        """Simulated corridor/gate events for the tractor, if any exist.

        Kept separate from the document read so the timeline can label anything
        sourced here as SIMULATED rather than corpus-evidenced.
        """
        if not vehicle_no:
            return []
        return await self._rows(
            """
            SELECT id, ts, gate_id, plate, event_type, trip_id, container_number,
                   bat_lane, source
              FROM core.gate_event
             WHERE regexp_replace(upper(COALESCE(plate, '')), '[^A-Z0-9]', '', 'g')
                   = regexp_replace(upper(:vn), '[^A-Z0-9]', '', 'g')
             ORDER BY ts DESC LIMIT :limit
            """,
            {"vn": vehicle_no, "limit": limit})
