"""UC-I Marine bathymetry — read repository + service (core.bathymetry_survey/_sounding).

Read-only orchestration for the depth soundings ingested by the shared marine upload
framework (services.marine.parsers.bathymetry_pdf / bathymetry_json -> repository.persist).
The WRITE path lives in :meth:`VesselCallRepository.persist`; this module owns the survey
list, the per-survey aggregates and the paginated sounding read.

Structured exactly like services/marine/sea_channel.py: a Repository holding the SQL and a
thin Service composing the ``{items,total,limit,offset,count}`` envelope.

SCALE NOTE — the one place this differs from its siblings. core.bathymetry_sounding holds
one row per plotted sounding: a single chart is 15k-30k rows and the reference corpus is
~190k. Every sounding read is therefore MANDATORILY scoped to one survey and hard-capped by
the router, and the aggregates are computed in SQL so a caller never has to pull rows to
count them.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.marine.bathymetry")

_SURVEY_COLUMNS = ("survey_id", "drawing_no", "section_label", "design_depth_m",
                   "survey_start", "survey_end", "survey_vessel", "file_path")
_SURVEY_SELECT = ", ".join(f"s.{c}" for c in _SURVEY_COLUMNS)
_SURVEY_LIKE = {"drawing_no": "drawing_no", "section": "section_label",
                "vessel": "survey_vessel"}
_SURVEY_SORTS = {"drawing_no": "s.drawing_no", "section_label": "s.section_label",
                 "design_depth_m": "s.design_depth_m", "survey_start": "s.survey_start",
                 "survey_end": "s.survey_end", "survey_id": "s.survey_id"}

_SOUNDING_COLUMNS = ("sounding_id", "survey_id", "easting_m", "northing_m", "lat", "lon",
                     "depth_m", "above_design", "page_x_pt", "page_y_pt", "import_file_id")
_SOUNDING_SELECT = ", ".join(f"b.{c}" for c in _SOUNDING_COLUMNS)
_SOUNDING_SORTS = {"depth_m": "b.depth_m", "easting_m": "b.easting_m",
                   "northing_m": "b.northing_m", "sounding_id": "b.sounding_id"}


def _f(v: Any) -> Optional[float]:
    return float(v) if v is not None else None


#: core.bathymetry_survey.survey_id is smallint. Passing a larger value to asyncpg raises
#: DataError ("value out of int16 range") — which would surface as a 500 for what is really
#: just a non-existent id. Out-of-range ids are treated as "no such survey" instead.
_SMALLINT_MIN, _SMALLINT_MAX = -32768, 32767


def _valid_survey_id(survey_id: int) -> bool:
    return isinstance(survey_id, int) and _SMALLINT_MIN <= survey_id <= _SMALLINT_MAX


class BathymetryRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ surveys
    def _survey_where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for key, col in _SURVEY_LIKE.items():
            val = filters.get(key)
            if val:
                conds.append(f"s.{col} ILIKE :{key}")
                params[key] = f"%{str(val).strip()}%"
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list_surveys(self, filters: Mapping[str, Any], *, sort: str,
                           direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._survey_where(filters)
        order_col = _SURVEY_SORTS.get(sort, "s.drawing_no")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        # sounding_count comes from a correlated aggregate rather than a join so a survey
        # with no soundings yet still lists (LEFT JOIN + GROUP BY would too, but this keeps
        # the row shape identical to the plain column list).
        sql = (f"SELECT {_SURVEY_SELECT}, "
               "  (SELECT count(*) FROM core.bathymetry_sounding b "
               "   WHERE b.survey_id = s.survey_id) AS sounding_count "
               f"FROM core.bathymetry_survey s {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, s.survey_id "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def count_surveys(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._survey_where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.bathymetry_survey s {clause}"),
                params)).scalar() or 0)

    async def get_survey(self, survey_id: int) -> Optional[dict]:
        if not _valid_survey_id(survey_id):
            return None
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SURVEY_SELECT} FROM core.bathymetry_survey s "
                "WHERE s.survey_id = :id"), {"id": survey_id})).mappings().first()
        return dict(row) if row else None

    # ------------------------------------------------------------------ stats
    async def survey_stats(self, survey_id: int) -> Optional[dict]:
        """Per-survey aggregates. None when the survey itself does not exist.

        Aggregated in SQL — a survey holds tens of thousands of soundings and a caller
        must never have to fetch them to count them. Depth extents are reported over the
        georeferenced and page-space rows alike; the coordinate bbox is over the
        georeferenced subset only, and is null when the chart carries no grid.
        """
        survey = await self.get_survey(survey_id)   # also rejects out-of-range ids
        if survey is None:
            return None
        sql = (
            "SELECT count(*) AS sounding_count, "
            "  count(*) FILTER (WHERE b.above_design) AS above_design_count, "
            "  count(*) FILTER (WHERE b.easting_m IS NOT NULL) AS georeferenced_count, "
            "  min(b.depth_m) AS min_depth_m, "
            "  max(b.depth_m) AS max_depth_m, "
            "  round(avg(b.depth_m)::numeric, 2) AS avg_depth_m, "
            "  min(b.easting_m) AS min_easting_m, max(b.easting_m) AS max_easting_m, "
            "  min(b.northing_m) AS min_northing_m, max(b.northing_m) AS max_northing_m "
            "FROM core.bathymetry_sounding b WHERE b.survey_id = :id"
        )
        async with get_engine(self._dsn).connect() as conn:
            r = (await conn.execute(text(sql), {"id": survey_id})).mappings().first()
        r = dict(r) if r else {}
        n = int(r.get("sounding_count") or 0)
        return {
            "survey_id": survey_id,
            "drawing_no": survey.get("drawing_no"),
            "design_depth_m": _f(survey.get("design_depth_m")),
            "sounding_count": n,
            "above_design_count": int(r.get("above_design_count") or 0),
            "georeferenced_count": int(r.get("georeferenced_count") or 0),
            "min_depth_m": _f(r.get("min_depth_m")),
            "max_depth_m": _f(r.get("max_depth_m")),
            "avg_depth_m": _f(r.get("avg_depth_m")),
            "bbox": None if not r.get("min_easting_m") else {
                "min_easting_m": _f(r.get("min_easting_m")),
                "max_easting_m": _f(r.get("max_easting_m")),
                "min_northing_m": _f(r.get("min_northing_m")),
                "max_northing_m": _f(r.get("max_northing_m")),
            },
        }

    # ------------------------------------------------------------------ soundings
    def _sounding_where(self, survey_id: int,
                        filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        """survey_id is ALWAYS present — an unscoped sounding scan is never permitted."""
        conds = ["b.survey_id = :survey_id"]
        params: dict[str, Any] = {"survey_id": survey_id}
        if filters.get("above_design") is not None:
            conds.append("b.above_design = :above_design")
            params["above_design"] = bool(filters["above_design"])
        if filters.get("min_depth") is not None:
            conds.append("b.depth_m >= :min_depth")
            params["min_depth"] = filters["min_depth"]
        if filters.get("max_depth") is not None:
            conds.append("b.depth_m <= :max_depth")
            params["max_depth"] = filters["max_depth"]
        if filters.get("georeferenced") is True:
            conds.append("b.easting_m IS NOT NULL")
        elif filters.get("georeferenced") is False:
            conds.append("b.easting_m IS NULL")
        return "WHERE " + " AND ".join(conds), params

    async def list_soundings(self, survey_id: int, filters: Mapping[str, Any], *,
                             sort: str, direction: str, limit: int,
                             offset: int) -> list[dict]:
        if not _valid_survey_id(survey_id):
            return []
        clause, params = self._sounding_where(survey_id, filters)
        order_col = _SOUNDING_SORTS.get(sort, "b.sounding_id")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SOUNDING_SELECT} FROM core.bathymetry_sounding b {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, b.sounding_id "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            rows = (await conn.execute(text(sql), params)).mappings().all()
        return [dict(r) for r in rows]

    async def count_soundings(self, survey_id: int, filters: Mapping[str, Any]) -> int:
        if not _valid_survey_id(survey_id):
            return 0
        clause, params = self._sounding_where(survey_id, filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.bathymetry_sounding b {clause}"),
                params)).scalar() or 0)


class BathymetryService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[BathymetryRepository] = None) -> None:
        self._repo = repository or BathymetryRepository(dsn)

    async def list_surveys(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_surveys(filters, sort=sort, direction=direction,
                                              limit=limit, offset=offset)
        total = await self._repo.count_surveys(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get_survey(self, survey_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.get_survey(survey_id)

    async def survey_stats(self, survey_id: int) -> Optional[Dict[str, Any]]:
        return await self._repo.survey_stats(survey_id)

    async def list_soundings(self, survey_id: int, filters: Mapping[str, Any], *,
                             sort: str, direction: str, limit: int,
                             offset: int) -> Dict[str, Any]:
        items = await self._repo.list_soundings(survey_id, filters, sort=sort,
                                                direction=direction, limit=limit,
                                                offset=offset)
        total = await self._repo.count_soundings(survey_id, filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}


__all__ = ["BathymetryRepository", "BathymetryService"]
