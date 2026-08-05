"""UC-I Marine pilotage — read repository + service (core.pilotage).

Read-only orchestration for the pilot-card movements ingested by the shared marine
upload framework (services.marine.parsers.pilot_card_xlsx → repository.persist). The
WRITE path lives in :meth:`VesselCallRepository.persist` (one transaction across all
targets); this module owns the list/get/stats reads, mirroring the vessel-call
repository/service split. Raw parameterised ``text()``; touches only ``core.pilotage``.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

from . import pilot_status as ps
from .projection import MarineProjection

log = get_logger("services.marine.pilotage")

_COLUMNS = (
    "pilotage_id", "movement_type", "call_id", "via_no", "imo_no", "vessel_name",
    "pilot_code", "vessel_condition", "from_berth_id", "to_berth_id", "draft_fwd_m",
    "draft_aft_m", "pilot_boarded_at", "first_line_at", "all_fast_at",
    "pilot_disembarked_at", "berth_vacated_at", "anchor_down_at", "anchor_up_at",
    "submitted_at", "extras", "import_file_id",
)
_SELECT = ", ".join(f"p.{c}" for c in _COLUMNS)

_EQ_FILTERS = ("movement_type", "imo_no", "pilot_code")
_LIKE_FILTERS = {"vessel": "vessel_name", "via": "via_no"}
_SORTS = {"submitted_at": "p.submitted_at", "pilot_boarded_at": "p.pilot_boarded_at",
          "movement_type": "p.movement_type", "vessel_name": "p.vessel_name",
          "pilotage_id": "p.pilotage_id"}


class PilotageRepository:
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
        # LIVE / DEMO provenance narrowing; None => unfiltered (byte-identical SQL).
        data_origin = filters.get("data_origin")
        if data_origin is not None:
            conds.append("p.data_origin = :data_origin")
            params["data_origin"] = data_origin
        clause = ("WHERE " + " AND ".join(conds)) if conds else ""
        return clause, params

    async def list_pilotage(self, filters: Mapping[str, Any], *, sort: str,
                            direction: str, limit: int, offset: int) -> list[dict]:
        clause, params = self._where(filters)
        order_col = _SORTS.get(sort, "p.submitted_at")
        order_dir = "ASC" if str(direction).lower() == "asc" else "DESC"
        params.update({"limit": limit, "offset": offset})
        sql = (f"SELECT {_SELECT} FROM core.pilotage p {clause} "
               f"ORDER BY {order_col} {order_dir} NULLS LAST, p.pilotage_id DESC "
               "LIMIT :limit OFFSET :offset")
        async with get_engine(self._dsn).connect() as conn:
            return [dict(r) for r in (await conn.execute(text(sql), params)).mappings().all()]

    async def count(self, filters: Mapping[str, Any]) -> int:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            return int((await conn.execute(
                text(f"SELECT count(*) FROM core.pilotage p {clause}"), params)).scalar() or 0)

    async def get(self, pilotage_id: int, *,
                  data_origin: Optional[str] = None) -> Optional[dict]:
        frag, extra = ("", {})
        if data_origin is not None:
            frag = " AND p.data_origin = :data_origin"
            extra = {"data_origin": data_origin}
        async with get_engine(self._dsn).connect() as conn:
            row = (await conn.execute(text(
                f"SELECT {_SELECT} FROM core.pilotage p WHERE p.pilotage_id = :id{frag}"),
                {"id": pilotage_id, **extra})).mappings().first()
        return dict(row) if row else None

    async def stats(self, filters: Mapping[str, Any]) -> dict:
        clause, params = self._where(filters)
        async with get_engine(self._dsn).connect() as conn:
            total = int((await conn.execute(
                text(f"SELECT count(*) FROM core.pilotage p {clause}"), params)).scalar() or 0)
            by_mv = (await conn.execute(text(
                f"SELECT p.movement_type, count(*) AS n FROM core.pilotage p {clause} "
                "GROUP BY p.movement_type ORDER BY p.movement_type"), params)).mappings().all()
            pilots = int((await conn.execute(
                text(f"SELECT count(DISTINCT p.pilot_code) FROM core.pilotage p {clause}"),
                params)).scalar() or 0)
        return {"total": total, "pilots": pilots,
                "by_movement": [{"movement_type": m["movement_type"], "count": int(m["n"])}
                                for m in by_mv]}


class PilotageService:
    def __init__(self, dsn: Optional[str] = None,
                 repository: Optional[PilotageRepository] = None,
                 projection: Optional[MarineProjection] = None) -> None:
        self._repo = repository or PilotageRepository(dsn)
        # Lifecycle comes from the shared Marine Projection Layer. This module owns no
        # query against core.vessel_call* and never calls the state engine.
        self._projection = projection or MarineProjection(dsn)

    async def _attach_workflow(self, rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        """Attach each movement's pilot workflow status, from the projection.

        Keyed on VIA rather than call_id: every pilot card carries a VIA, but
        ``core.pilotage.call_id`` resolves only for rows imported AFTER the matching call
        existed, so keying on it would leave older cards permanently unenriched.

        One batched lookup per page — never per row. Row set, order and key set are all
        unchanged, so PilotageOut is untouched.
        """
        if not rows:
            return rows
        states = await self._projection.by_vias([r.get("via_no") for r in rows])
        return [ps.apply(r, states.get(str(r.get("via_no") or ""))) for r in rows]

    async def list_pilotage(self, filters: Mapping[str, Any], *, sort: str, direction: str,
                            limit: int, offset: int) -> Dict[str, Any]:
        items = await self._repo.list_pilotage(filters, sort=sort, direction=direction,
                                               limit=limit, offset=offset)
        items = await self._attach_workflow(items)
        total = await self._repo.count(filters)
        return {"items": items, "total": total, "limit": limit, "offset": offset,
                "count": len(items)}

    async def get(self, pilotage_id: int, *,
                  data_origin: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """One movement, enriched exactly as the list rows are — see :meth:`_attach_workflow`.

        ``data_origin`` narrows the read to one provenance (LIVE=API / DEMO=MANUAL); the
        workflow attach runs the same either way, so a single card cannot disagree with the
        same card in the list.
        """
        row = await self._repo.get(pilotage_id, data_origin=data_origin)
        if row is None:
            return None
        return (await self._attach_workflow([row]))[0]

    async def stats(self, filters: Mapping[str, Any]) -> Dict[str, Any]:
        return await self._repo.stats(filters)
