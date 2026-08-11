"""Vehicle -> transporter registry persistence (UC3-004).

Read-only over ``core.transporter_vehicle`` joined to ``core.transporter``. The
rows are written by scripts/seed_uc3_004_vehicle_registry.py; nothing here
inserts, updates or deletes.

Provenance is carried through verbatim (migration 0134): DOCUMENT_EVIDENCED rows
expose the ``source_ref`` gate document the mapping was read from, SYNTHETIC rows
expose ``assumption_ref`` (A-G6). The API never collapses the two.
"""
from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.vehicle_registry.repository")

VALID_PROVENANCE = ("DOCUMENT_EVIDENCED", "SYNTHETIC")

#: Seed that generated the SYNTHETIC half — surfaced so a reader can reproduce it.
SYNTHETIC_SEED = "UC3-004:A-G6:v1"

_SELECT = """
    SELECT tv.id,
           tv.vehicle_no,
           tv.vehicle_no_norm,
           tv.driver_id,
           tv.provenance,
           tv.assumption_ref,
           tv.source_ref,
           tv.transporter_id,
           t.company_id,
           t.company_name        AS transporter,
           t.contact_person      AS transporter_contact
      FROM core.transporter_vehicle tv
      JOIN core.transporter t ON t.id = tv.transporter_id
"""


def norm_plate(plate: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (plate or "").upper())


class VehicleRegistryRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def list_mappings(self, *, provenance: Optional[str], q: Optional[str],
                            limit: int, offset: int) -> list[dict]:
        conds, p = [], {"limit": limit, "offset": offset}
        if provenance:
            conds.append("tv.provenance = :prov")
            p["prov"] = provenance
        if q:
            conds.append("(tv.vehicle_no_norm LIKE :q OR upper(t.company_name) LIKE :q)")
            p["q"] = f"%{norm_plate(q)}%" if norm_plate(q) else f"%{q.upper()}%"
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        return await self._rows(
            f"{_SELECT}{where} ORDER BY tv.vehicle_no_norm LIMIT :limit OFFSET :offset", p)

    async def count_mappings(self, *, provenance: Optional[str], q: Optional[str]) -> int:
        conds, p = [], {}
        if provenance:
            conds.append("tv.provenance = :prov")
            p["prov"] = provenance
        if q:
            conds.append("(tv.vehicle_no_norm LIKE :q OR upper(t.company_name) LIKE :q)")
            p["q"] = f"%{norm_plate(q)}%" if norm_plate(q) else f"%{q.upper()}%"
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        rows = await self._rows(
            "SELECT count(*) AS n FROM core.transporter_vehicle tv "
            "JOIN core.transporter t ON t.id = tv.transporter_id" + where, p)
        return int(rows[0]["n"]) if rows else 0

    async def by_vehicle(self, plate: str) -> Optional[dict]:
        rows = await self._rows(
            f"{_SELECT} WHERE tv.vehicle_no_norm = :p LIMIT 1", {"p": norm_plate(plate)})
        return rows[0] if rows else None

    async def provenance_summary(self) -> list[dict]:
        return await self._rows(
            "SELECT provenance, count(*) AS mappings "
            "FROM core.transporter_vehicle GROUP BY provenance ORDER BY provenance")
