"""UC-I Marine sea-channel — read repository + service (core.sea_channel).

Read-only orchestration for the channel geometry ingested by the shared marine upload
framework (services.marine.parsers.sea_channel_shp → repository.persist). The WRITE path
lives in :meth:`VesselCallRepository.persist`; this module owns list/get/stats reads plus
a GeoJSON FeatureCollection projection for the map.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.marine.sea_channel")

_COLUMNS = ("channel_id", "name", "section_label", "area_ha", "length_m",
            "geom_geojson", "import_file_id")
_SELECT = ", ".join(f"s.{c}" for c in _COLUMNS)
_LIKE_FILTERS = {"name": "name", "section": "section_label"}
_SORTS = {"name": "s.name", "area_ha": "s.area_ha", "length_m": "s.length_m",
          "channel_id": "s.channel_id"}


class SeaChannelRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for key, col in _LIKE_FILTERS.items():
            val = filters.get(key)
            if val:
                conds.append(f"s.{col} ILIKE :{key}")
                params[key] = f"%{str(val).strip()}%"
        # LIVE / DEMO provenance narrowing; None => unfiltered (byte-identical SQL).
        data_origin = filters.get("data_origin")
        if data_origin is not None:
            conds.append("s.data_origin = :data_origin")
            params["data_origin"] = data_origin
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list_channels(self, filters: Mapping[str, Any], *, sort: str,
                            direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "s.name")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT} FROM core.sea_channel s {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, s.channel_id "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.sea_channel s {clause}"), params)).scalar() or 0)

    async def get(self, channel_id: int, *,
                  data_origin: Optional[str] = None) -> Optional[dict]:
        frag, extra = ("", {})
        if data_origin is not None:
            frag = " AND s.data_origin = :data_origin"
            extra = {"data_origin": data_origin}
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT} FROM core.sea_channel s WHERE s.channel_id = :id{frag}"),
                {"id": channel_id, **extra})).mappings().first()
        return dict(row) if row else None

    async def stats(self, filters: Mapping[str, Any]) -> dict:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            total = int((await conn.execute(
                text(f"SELECT count(*) FROM core.sea_channel s {clause}"), params)).scalar() or 0)
            by_name = (await conn.execute(text(
                f"SELECT s.name, count(*) AS n, round(sum(s.area_ha)::numeric, 2) AS area_ha "
                f"FROM core.sea_channel s {clause} GROUP BY s.name ORDER BY n DESC, s.name"),
                params)).mappings().all()
        return {"total": total,
                "by_name": [{"name": r["name"], "count": int(r["n"]),
                             "area_ha": float(r["area_ha"]) if r["area_ha"] is not None else None}
                            for r in by_name]}


class SeaChannelService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[SeaChannelRepository] = None) -> None:
        self._repo = repository or SeaChannelRepository(dsn)

    async def list_channels(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                            limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_channels(filters, sort=sort, direction=direction,
                                               limit=limit, offset=offset)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, channel_id: int, *,
                  data_origin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        return await self._repo.get(channel_id, data_origin=data_origin)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)

    async def geojson(self, filters: Mapping[str, Any], *, limit: int) -> Dict[str, Any]:
        """A GeoJSON FeatureCollection (WGS84) for the map overlay."""
        items = await self._repo.list_channels(filters, sort="name", direction="asc",
                                                limit=limit, offset=0)
        features = [{
            "type": "Feature",
            "geometry": r.get("geom_geojson"),
            "properties": {"channel_id": r["channel_id"], "name": r["name"],
                           "section_label": r.get("section_label"),
                           "area_ha": float(r["area_ha"]) if r.get("area_ha") is not None else None,
                           "length_m": float(r["length_m"]) if r.get("length_m") is not None else None},
        } for r in items if r.get("geom_geojson")]
        return {"type": "FeatureCollection", "features": features, "count": len(features)}
