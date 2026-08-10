"""UC-I Marine vessel master — read repository + service (core.vessel).

Read-only orchestration for the port-approved vessel registry ingested from VESPRO by the
shared marine upload framework (services.marine.parsers.vespro → repository.persist). The
WRITE path lives in :meth:`VesselCallRepository.persist`; this module owns the
list/get/stats reads, mirroring the port-craft and pilotage repository/service split.

Grain note: a VESSEL is a hull (keyed on IMO), NOT a port visit. `core.vessel_call` is the
visit. The two are joined by imo_no and must never be merged — a hull has many calls, and
a call may exist before its hull is known (a CALINF-seeded call carries no vessel row yet).

P&I insurance is fanned out to core.vessel_insurance by the same VESPRO record, so it is
returned only on the SINGLE-vessel read: it is 0..n rows per hull and would turn the list
query into a fan-out join for data the table view does not show.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.marine.vessel")

_COLUMNS = (
    "imo_no", "vessel_name", "call_sign", "flag", "vessel_type", "mtmv", "loa_m", "beam_m",
    "lbp_m", "max_draft_m", "grt", "nrt", "dwt", "teu_capacity", "mmsi", "engine_type",
    "num_engines", "propulsion_type", "num_propellers", "max_speed_kn", "bow_thruster",
    "stern_thruster", "built_date", "reg_port", "owner_name", "email", "vespro_ref",
    "updated_at",
)
_SELECT = ", ".join(f"v.{c}" for c in _COLUMNS)
_EQ_FILTERS = ("flag", "vessel_type")
_LIKE_FILTERS = {"name": "vessel_name", "imo": "imo_no", "owner": "owner_name",
                 "call_sign": "call_sign"}
_SORTS = {"vessel_name": "v.vessel_name", "imo_no": "v.imo_no", "loa_m": "v.loa_m",
          "max_draft_m": "v.max_draft_m", "grt": "v.grt", "dwt": "v.dwt",
          "teu_capacity": "v.teu_capacity", "updated_at": "v.updated_at"}

_INSURANCE_SELECT = (
    "SELECT i.pi_club, i.valid_until FROM core.vessel_insurance i "
    "WHERE i.imo_no = :imo_no ORDER BY i.pi_club"
)


class VesselRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    def _where(self, filters: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        conds: list[str] = []
        params: dict[str, Any] = {}
        for col in _EQ_FILTERS:
            val = filters.get(col)
            if val is not None:
                conds.append(f"v.{col} = :{col}")
                params[col] = val
        for key, col in _LIKE_FILTERS.items():
            val = filters.get(key)
            if val:
                conds.append(f"v.{col} ILIKE :{key}")
                params[key] = f"%{str(val).strip()}%"
        # Demo replay date filter (UC1-004): core.vessel carries no port-visit date, only
        # `updated_at` (last registry sync), so that is the one honest column to range-filter
        # on. Same >=/<= idiom as VesselCallRepository's eta_from/eta_to.
        if filters.get("date_from") is not None:
            conds.append("v.updated_at >= :date_from")
            params["date_from"] = filters["date_from"]
        if filters.get("date_to") is not None:
            conds.append("v.updated_at <= :date_to")
            params["date_to"] = filters["date_to"]
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list_vessels(self, filters: Mapping[str, Any], *, sort: str,
                           direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "v.vessel_name")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT} FROM core.vessel v {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, v.imo_no "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.vessel v {clause}"), params)).scalar() or 0)

    async def get(self, imo_no: str) -> Optional[dict]:
        """One hull + its P&I cover. Returns None when the IMO is unknown."""
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT} FROM core.vessel v WHERE v.imo_no = :imo_no"),
                {"imo_no": imo_no})).mappings().first()
            if row is None:
                return None
            cover = (await conn.execute(text(_INSURANCE_SELECT),
                                        {"imo_no": imo_no})).mappings().all()
        out = dict(row)
        out["insurance"] = [{"pi_club": c["pi_club"], "valid_until": c["valid_until"]}
                            for c in cover]
        return out

    async def stats(self, filters: Mapping[str, Any]) -> dict:
        """Registry totals + the completeness counters the berth-fit engine depends on.

        `with_dimensions` is the one that matters operationally: a hull with no LOA/beam
        /draft cannot be checked against a berth, so the count is a data-readiness signal,
        not decoration.
        """
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                "SELECT count(*) AS total, "
                "  count(*) FILTER (WHERE v.loa_m IS NOT NULL AND v.beam_m IS NOT NULL "
                "                     AND v.max_draft_m IS NOT NULL) AS with_dimensions, "
                "  count(*) FILTER (WHERE v.teu_capacity IS NOT NULL) AS with_teu, "
                "  count(*) FILTER (WHERE v.mmsi IS NOT NULL) AS with_mmsi, "
                "  round(avg(v.loa_m)::numeric, 2) AS avg_loa_m, "
                "  max(v.max_draft_m) AS max_draft_m "
                f"FROM core.vessel v {clause}"), params)).mappings().first() or {}
            by_flag = (await conn.execute(text(
                f"SELECT v.flag, count(*) AS n FROM core.vessel v {clause} "
                "GROUP BY v.flag ORDER BY n DESC, v.flag"), params)).mappings().all()
        return {
            "total": int(row.get("total") or 0),
            "with_dimensions": int(row.get("with_dimensions") or 0),
            "with_teu": int(row.get("with_teu") or 0),
            "with_mmsi": int(row.get("with_mmsi") or 0),
            "avg_loa_m": float(row["avg_loa_m"]) if row.get("avg_loa_m") is not None else None,
            "max_draft_m": float(row["max_draft_m"]) if row.get("max_draft_m") is not None else None,
            "by_flag": [{"flag": f["flag"], "count": int(f["n"])} for f in by_flag],
        }


class VesselService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[VesselRepository] = None) -> None:
        self._repo = repository or VesselRepository(dsn)

    async def list_vessels(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                           limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_vessels(filters, sort=sort, direction=direction,
                                              limit=limit, offset=offset)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, imo_no: str) -> Optional[Dict[str, Any]]:
        return await self._repo.get(imo_no)

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
