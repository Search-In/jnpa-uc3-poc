"""UC-I Marine port-craft register — read repository + service (core.port_craft).

Read-only orchestration for the tug/launch fleet register ingested by the shared marine
upload framework (services.marine.parsers.port_craft_pdf → repository.persist). The WRITE
path lives in :meth:`VesselCallRepository.persist`; this module owns the list/get/stats
reads, mirroring the pilotage repository/service split.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.marine.port_craft")

_COLUMNS = (
    "craft_id", "name", "craft_type", "owned_or_hired", "owner_name", "year_built",
    "loa_m", "breadth_m", "draft_m", "main_engines", "bollard_pull_t", "design_speed_kn",
    "import_file_id", "extras",
)
_SELECT = ", ".join(f"p.{c}" for c in _COLUMNS)
_EQ_FILTERS = ("craft_type", "owned_or_hired")
_LIKE_FILTERS = {"name": "name", "owner": "owner_name"}
_SORTS = {"name": "p.name", "craft_type": "p.craft_type", "loa_m": "p.loa_m",
          "bollard_pull_t": "p.bollard_pull_t", "craft_id": "p.craft_id"}


class PortCraftRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"p.{col} = :{col}")
                params[col] = val
        for key, col in _LIKE_FILTERS.items():
            val = filters.get(key)
            if val:
                conds.append(f"p.{col} ILIKE :{key}")
                params[key] = f"%{str(val).strip()}%"
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list_craft(self, filters: Mapping[str, Any], *, sort: str,
                         direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "p.name")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT} FROM core.port_craft p {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, p.craft_id "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.port_craft p {clause}"), params)).scalar() or 0)

    async def get(self, craft_id: int) -> Optional[dict]:
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT} FROM core.port_craft p WHERE p.craft_id = :id"),
                {"id": craft_id})).mappings().first()
        return dict(row) if row else None

    async def stats(self, filters: Mapping[str, Any]) -> dict:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            total = int((await conn.execute(
                text(f"SELECT count(*) FROM core.port_craft p {clause}"), params)).scalar() or 0)
            by_type = (await conn.execute(text(
                f"SELECT p.craft_type, count(*) AS n FROM core.port_craft p {clause} "
                "GROUP BY p.craft_type ORDER BY n DESC, p.craft_type"), params)).mappings().all()
            by_oh = (await conn.execute(text(
                f"SELECT p.owned_or_hired, count(*) AS n FROM core.port_craft p {clause} "
                "GROUP BY p.owned_or_hired ORDER BY p.owned_or_hired"), params)).mappings().all()
        return {"total": total,
                "by_type": [{"craft_type": t["craft_type"], "count": int(t["n"])} for t in by_type],
                "by_ownership": [{"owned_or_hired": o["owned_or_hired"], "count": int(o["n"])} for o in by_oh]}


class PortCraftService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[PortCraftRepository] = None) -> None:
        self._repo = repository or PortCraftRepository(dsn)

    async def list_craft(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                         limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_craft(filters, sort=sort, direction=direction,
                                            limit=limit, offset=offset)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, craft_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get(craft_id)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
