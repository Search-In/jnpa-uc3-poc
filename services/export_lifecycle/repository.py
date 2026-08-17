"""Export lifecycle persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL for ``core.export_booking`` /
``core.export_booking_event`` (migration 0115).

Discipline copied verbatim from ``services.container_job.repository``: every
status move is applied under ``SELECT ... FOR UPDATE`` and written together with
its history row in ONE transaction, so a concurrent double-step cannot interleave
and the audit trail can never disagree with the row it describes.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from gateway.datewindow import window_cond  # GAP-DATE-01

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.export_lifecycle.repository")

_CREATE_COLS = ("booking_no", "container_number", "shipping_line", "vessel_name",
                "voyage_no", "via_no", "pod", "terminal", "cfs_code",
                "declared_gross_kg", "created_by")


class ExportRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ helpers
    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _one(self, sql: str, params: Mapping[str, Any] | None = None) -> Optional[dict]:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    # -------------------------------------------------------------------- reads
    async def get(self, booking_id: int) -> Optional[dict]:
        return await self._one(
            "SELECT * FROM core.export_booking WHERE id = :i", {"i": booking_id})

    async def by_booking_no(self, booking_no: str) -> Optional[dict]:
        return await self._one(
            "SELECT * FROM core.export_booking WHERE booking_no = :b", {"b": booking_no})

    async def open_for_container(self, container_number: str) -> Optional[dict]:
        """The one non-terminal booking for a container (uq_export_booking_open)."""
        return await self._one(
            "SELECT * FROM core.export_booking "
            "WHERE container_number = :c AND status NOT IN ('LOADED','CANCELLED') "
            "ORDER BY id DESC LIMIT 1", {"c": container_number})

    async def list(self, *, status: Optional[str] = None,
                   container_number: Optional[str] = None,
                   via_no: Optional[str] = None,
                   limit: int = 100, offset: int = 0,
                           window: Any = None,
                           date_col: Optional[str] = None,) -> tuple[list[dict], int]:
        where, params = [], {"lim": limit, "off": offset}
        if status:
            where.append("status = :st")
            params["st"] = status
        if container_number:
            where.append("container_number = :cn")
            params["cn"] = container_number
        if via_no:
            where.append("via_no = :via")
            params["via"] = via_no
        # GAP-DATE-01: appended before the WHERE is assembled — after it,
        # this is a silent no-op. `date_col` is named by the caller.
        _wc = window_cond(window, date_col, params) if date_col else None
        if _wc:
            where.append(_wc)

        clause = (" WHERE " + " AND ".join(where)) if where else ""
        items = await self._rows(
            f"SELECT * FROM core.export_booking{clause} ORDER BY id DESC "
            f"LIMIT :lim OFFSET :off", params)
        async with get_engine(self._dsn).connect() as conn:
            total = int((await conn.execute(
                text(f"SELECT count(*) FROM core.export_booking{clause}"),
                params)).scalar() or 0)
        return items, total

    async def events(self, booking_id: int) -> list[dict]:
        return await self._rows(
            "SELECT * FROM core.export_booking_event WHERE booking_id = :i "
            "ORDER BY id", {"i": booking_id})

    async def summary(self) -> dict:
        rows = await self._rows(
            "SELECT status, count(*) AS n FROM core.export_booking GROUP BY status")
        by_status = {r["status"]: int(r["n"]) for r in rows}
        totals = await self._one(
            "SELECT count(*) AS bookings, "
            "       count(vgm_kg) AS with_vgm, "
            "       count(leo_no) AS with_leo, "
            "       count(loaded_at) AS loaded "
            "FROM core.export_booking")
        return {"by_status": by_status, **{k: int(v or 0) for k, v in (totals or {}).items()}}

    # ------------------------------------------------------------------- create
    async def create(self, fields: Mapping[str, Any]) -> dict:
        cols = [c for c in _CREATE_COLS if fields.get(c) is not None]
        placeholders = ", ".join(f":{c}" for c in cols)
        sql = (f"INSERT INTO core.export_booking ({', '.join(cols)}) "
               f"VALUES ({placeholders}) RETURNING *")
        async with get_engine(self._dsn).begin() as conn:
            row = dict((await conn.execute(
                text(sql), {c: fields[c] for c in cols})).mappings().first())
            await self._event(conn, row["id"], event="BOOKED", old=None,
                              new="BOOKED", detail={"booking_no": row["booking_no"]},
                              actor=fields.get("created_by"))
        return row

    # -------------------------------------------------------------------- steps
    async def advance(self, booking_id: int, *, new_status: str, event: str,
                      set_fields: Mapping[str, Any], allowed_from: set[str],
                      detail: Optional[Mapping[str, Any]] = None,
                      actor: Optional[str] = None,
                      actor_role: Optional[str] = None) -> dict:
        """Apply one export step atomically.

        Returns ``{"ok": True, "booking": row}`` or
        ``{"ok": False, "reason": ..., "booking": row|None}`` — the service maps
        those onto domain exceptions, so SQL never leaks upward.
        """
        assigns = ", ".join(f"{k} = :{k}" for k in set_fields)
        sql_update = (
            f"UPDATE core.export_booking SET status = :__new"
            f"{', ' + assigns if assigns else ''}, updated_at = now() "
            f"WHERE id = :__id RETURNING *")
        async with get_engine(self._dsn).begin() as conn:
            cur = (await conn.execute(
                text("SELECT * FROM core.export_booking WHERE id = :i FOR UPDATE"),
                {"i": booking_id})).mappings().first()
            if cur is None:
                return {"ok": False, "reason": "booking_not_found", "booking": None}
            old_status = cur["status"]
            if old_status not in allowed_from:
                return {"ok": False, "reason": "illegal_transition",
                        "booking": dict(cur), "from": old_status}
            params: dict[str, Any] = {"__id": booking_id, "__new": new_status}
            params.update(set_fields)
            row = dict((await conn.execute(text(sql_update), params)).mappings().first())
            await self._event(conn, booking_id, event=event, old=old_status,
                              new=new_status, detail=detail, actor=actor,
                              actor_role=actor_role)
        return {"ok": True, "booking": row, "from": old_status}

    @staticmethod
    async def _event(conn, booking_id: int, *, event: str, old: Optional[str],
                     new: Optional[str], detail: Optional[Mapping[str, Any]] = None,
                     actor: Optional[str] = None,
                     actor_role: Optional[str] = None) -> None:
        await conn.execute(
            text("INSERT INTO core.export_booking_event "
                 "(booking_id, event, old_status, new_status, detail, actor, actor_role) "
                 "VALUES (:b, :e, :o, :n, CAST(:d AS jsonb), :a, :r)"),
            {"b": booking_id, "e": event, "o": old, "n": new,
             "d": json.dumps(dict(detail or {}), default=str),
             "a": actor, "r": actor_role})

    # --------------------------------------------------------------- cargo link
    async def upsert_cargo_for_export(self, container_number: str, *,
                                      lifecycle_status: str) -> None:
        """Keep core.cargo in step with the export leg.

        core.cargo stays the single source of truth for "where is this box in its
        life", exactly as on the import side — the booking row holds the
        documentary facts, the cargo row holds the state. An export container that
        has never been seen before is created here rather than 404ing the caller.
        """
        if not container_number:
            return
        async with get_engine(self._dsn).begin() as conn:
            await conn.execute(
                text("INSERT INTO core.cargo (container_number, customs_status, "
                     "                        is_released, lifecycle_status, direction) "
                     "VALUES (:c, 'PENDING', false, :ls, 'EXPORT') "
                     "ON CONFLICT (container_number) DO UPDATE "
                     "SET lifecycle_status = EXCLUDED.lifecycle_status, "
                     "    direction = 'EXPORT', updated_at = now()"),
                {"c": container_number, "ls": lifecycle_status})


__all__ = ["ExportRepository"]
