"""Persistence for UC-3 yard capacity + truck-arrival management (migration 0144).

The only layer that speaks SQL for this module. Two rules are load-bearing:

* **Every occupancy change is audited.** :meth:`adjust_occupancy` writes the new
  ``core.yard_capacity_state.occupied_slots`` and the matching
  ``core.yard_capacity_event`` row inside ONE transaction, so an occupancy value
  can never exist without the event that explains it.
* **Capacity prefers the real master.** :meth:`capacity_for` reads
  ``core.yard_block`` (migration 0130 — the yard capacity master) first and only
  falls back to the declared ``capacity_slots`` when that master carries no rows
  for the terminal, reporting which one answered. Nothing here ever writes to
  ``core.yard_block``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.yard_capacity.repository")

_STATE_COLS = ("yard_id, terminal_code, name, capacity_slots, occupied_slots, "
               "high_threshold_pct, critical_threshold_pct, source, source_note, "
               "active, created_at, updated_at")

_HOLD_COLS = ("id, device_id, plate, driver_id, driver_name, source, gate_id, eta_s, "
              "yard_id, yard_utilization_pct, status, reason, recommended_facility_id, "
              "recommended_facility_name, facility_available, facility_lat, facility_lon, "
              "estimated_wait_min, alert_id, notified, release_notified, detail, "
              "held_at, released_at, updated_at")


class YardCapacityRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ util
    async def _rows(self, sql: str, params: Mapping[str, Any] | None = None) -> list[dict]:
        async with get_engine(self._dsn).connect() as conn:
            res = await conn.execute(text(sql), dict(params or {}))
            return [dict(r) for r in res.mappings().all()]

    async def _one(self, sql: str, params: Mapping[str, Any] | None = None) -> Optional[dict]:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    # ----------------------------------------------------------- yard state
    async def list_yards(self) -> list[dict]:
        return await self._rows(
            f"SELECT {_STATE_COLS} FROM core.yard_capacity_state "
            "WHERE active IS TRUE ORDER BY yard_id")

    async def get_yard(self, yard_id: str) -> Optional[dict]:
        return await self._one(
            f"SELECT {_STATE_COLS} FROM core.yard_capacity_state WHERE yard_id = :y",
            {"y": yard_id})

    async def capacity_for(self, terminal_code: str) -> Optional[int]:
        """SUM(core.yard_block.capacity_teus) for a terminal, or None.

        ``core.yard_block`` is the capacity master (migration 0130). When JNPA
        loads a real block layout the denominator becomes measured with no code
        change; until then this returns None and the caller uses the declared
        figure AND says it is declared. Never raises: an un-migrated database
        must not break the yard board.
        """
        try:
            row = await self._one(
                "SELECT SUM(capacity_teus)::int AS cap FROM core.yard_block "
                "WHERE active IS TRUE AND capacity_teus IS NOT NULL "
                "AND upper(terminal) = upper(:t)",
                {"t": terminal_code})
        except Exception as exc:  # noqa: BLE001 — table may not exist yet
            log.debug("yard_capacity.block_master_unavailable", error=str(exc))
            return None
        cap = (row or {}).get("cap")
        return int(cap) if cap else None

    async def adjust_occupancy(
        self,
        *,
        yard_id: str,
        delta_slots: Optional[int] = None,
        set_occupied: Optional[int] = None,
        event_type: str,
        reason: Optional[str],
        actor: Optional[str],
        status_fn,
        detail: Optional[dict] = None,
    ) -> Optional[dict]:
        """Apply an occupancy change and audit it in ONE transaction.

        Exactly one of ``delta_slots`` / ``set_occupied`` is used. The new value
        is clamped into ``[0, capacity]`` by SQL (``greatest``/``least``) so the
        table's own CHECK can never be the thing that fails a demo action — an
        over-release simply lands at 0 and an over-fill at capacity, and the
        audit row records the clamped before/after.

        ``status_fn(pct) -> str`` is the caller's band classifier, so the audited
        status is the SAME function the API and the dashboard use rather than a
        second copy of the thresholds living in SQL.

        Returns the updated state row plus the event, or None for an unknown yard.
        """
        async with get_engine(self._dsn).begin() as conn:
            cur = (await conn.execute(
                text("SELECT capacity_slots, occupied_slots FROM core.yard_capacity_state "
                     "WHERE yard_id = :y FOR UPDATE"),
                {"y": yard_id})).mappings().first()
            if cur is None:
                return None
            capacity = int(cur["capacity_slots"])
            before = int(cur["occupied_slots"])
            if set_occupied is not None:
                target = int(set_occupied)
            else:
                target = before + int(delta_slots or 0)
            after = max(0, min(capacity, target))

            await conn.execute(
                text("UPDATE core.yard_capacity_state "
                     "SET occupied_slots = :o, source = 'DEMO_CONTROL' WHERE yard_id = :y"),
                {"o": after, "y": yard_id})

            pct = round(100.0 * after / capacity, 2) if capacity else 0.0
            status = status_fn(pct)
            ev = (await conn.execute(
                text("INSERT INTO core.yard_capacity_event "
                     "(yard_id, event_type, delta_slots, occupied_before, occupied_after, "
                     " capacity_slots, utilization_pct, status, reason, actor, detail) "
                     "VALUES (:y, :et, :d, :b, :a, :c, :p, :s, :r, :actor, CAST(:detail AS jsonb)) "
                     "RETURNING id, created_at"),
                {"y": yard_id, "et": event_type, "d": after - before, "b": before,
                 "a": after, "c": capacity, "p": pct, "s": status,
                 "r": reason, "actor": actor,
                 "detail": json.dumps(detail or {})})).mappings().first()

        row = await self.get_yard(yard_id)
        if row is not None:
            row = dict(row)
            row["last_event"] = {
                "id": (ev or {}).get("id"), "event_type": event_type,
                "delta_slots": after - before, "occupied_before": before,
                "occupied_after": after, "utilization_pct": pct, "status": status,
                "reason": reason, "actor": actor,
                "created_at": (ev or {}).get("created_at"),
            }
        return row

    async def recent_events(self, yard_id: str, limit: int = 25) -> list[dict]:
        return await self._rows(
            "SELECT id, yard_id, event_type, delta_slots, occupied_before, occupied_after, "
            "       capacity_slots, utilization_pct, status, reason, actor, detail, created_at "
            "FROM core.yard_capacity_event WHERE yard_id = :y "
            "ORDER BY id DESC LIMIT :lim",
            {"y": yard_id, "lim": limit})

    # ---------------------------------------------------------------- holds
    async def active_holds(self, yard_id: Optional[str] = None,
                           limit: int = 500) -> list[dict]:
        where = "WHERE status = 'HOLD_AT_PARKING'"
        params: dict[str, Any] = {"lim": limit}
        if yard_id:
            where += " AND yard_id = :y"
            params["y"] = yard_id
        return await self._rows(
            f"SELECT {_HOLD_COLS} FROM core.truck_arrival_hold {where} "
            "ORDER BY held_at ASC LIMIT :lim", params)

    async def holds(self, *, yard_id: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 200) -> list[dict]:
        clauses, params = [], {"lim": limit}
        if yard_id:
            clauses.append("yard_id = :y")
            params["y"] = yard_id
        if status:
            clauses.append("status = :st")
            params["st"] = status
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return await self._rows(
            f"SELECT {_HOLD_COLS} FROM core.truck_arrival_hold {where} "
            "ORDER BY held_at DESC LIMIT :lim", params)

    async def create_hold(self, row: Mapping[str, Any]) -> Optional[dict]:
        """Insert one hold. Returns None when the device already has an active
        hold (the partial unique index absorbs the race), so a concurrent
        evaluation can never double-hold a truck."""
        params = dict(row)
        params["detail"] = json.dumps(params.get("detail") or {})
        sql = (
            "INSERT INTO core.truck_arrival_hold "
            "(device_id, plate, driver_id, driver_name, source, gate_id, eta_s, yard_id, "
            " yard_utilization_pct, status, reason, recommended_facility_id, "
            " recommended_facility_name, facility_available, facility_lat, facility_lon, "
            " estimated_wait_min, alert_id, detail) "
            "VALUES (:device_id, :plate, :driver_id, :driver_name, :source, :gate_id, :eta_s, "
            "        :yard_id, :yard_utilization_pct, 'HOLD_AT_PARKING', :reason, "
            "        :recommended_facility_id, :recommended_facility_name, :facility_available, "
            "        :facility_lat, :facility_lon, :estimated_wait_min, :alert_id, "
            "        CAST(:detail AS jsonb)) "
            "ON CONFLICT DO NOTHING "
            f"RETURNING {_HOLD_COLS}"
        )
        async with get_engine(self._dsn).begin() as conn:
            res = await conn.execute(text(sql), params)
            out = res.mappings().first()
            if out is None:
                return None
            out = dict(out)
            await conn.execute(
                text("INSERT INTO core.truck_arrival_hold_event "
                     "(hold_id, device_id, action, actor, detail) "
                     "VALUES (:h, :d, 'HELD', :actor, CAST(:detail AS jsonb))"),
                {"h": out["id"], "d": out["device_id"],
                 "actor": params.get("actor"), "detail": params["detail"]})
        return out

    async def mark_notified(self, hold_id: int, device_id: str, *, delivered: bool,
                            detail: Optional[dict] = None,
                            release: bool = False) -> None:
        """Record the driver-notification outcome for a hold (or its release)."""
        col = "release_notified" if release else "notified"
        ok = "RELEASE_NOTIFIED" if release else "NOTIFIED"
        bad = "RELEASE_NOTIFY_FAILED" if release else "NOTIFY_FAILED"
        async with get_engine(self._dsn).begin() as conn:
            if delivered:
                await conn.execute(
                    text(f"UPDATE core.truck_arrival_hold SET {col} = true WHERE id = :h"),
                    {"h": hold_id})
            await conn.execute(
                text("INSERT INTO core.truck_arrival_hold_event "
                     "(hold_id, device_id, action, detail) "
                     "VALUES (:h, :d, :a, CAST(:detail AS jsonb))"),
                {"h": hold_id, "d": device_id, "a": ok if delivered else bad,
                 "detail": json.dumps(detail or {})})

    async def release_holds(self, hold_ids: Sequence[int], *, actor: Optional[str],
                            reason: Optional[str]) -> list[dict]:
        """Flip holds to RELEASED and audit each one. Returns the released rows."""
        ids = [int(i) for i in hold_ids]
        if not ids:
            return []
        async with get_engine(self._dsn).begin() as conn:
            res = await conn.execute(
                text("UPDATE core.truck_arrival_hold "
                     "SET status = 'RELEASED', released_at = now() "
                     "WHERE id = ANY(:ids) AND status = 'HOLD_AT_PARKING' "
                     f"RETURNING {_HOLD_COLS}"),
                {"ids": ids})
            rows = [dict(r) for r in res.mappings().all()]
            for r in rows:
                await conn.execute(
                    text("INSERT INTO core.truck_arrival_hold_event "
                         "(hold_id, device_id, action, actor, detail) "
                         "VALUES (:h, :d, 'RELEASED', :actor, CAST(:detail AS jsonb))"),
                    {"h": r["id"], "d": r["device_id"], "actor": actor,
                     "detail": json.dumps({"reason": reason})})
        return rows

    async def hold_events(self, device_id: str, limit: int = 50) -> list[dict]:
        return await self._rows(
            "SELECT e.id, e.hold_id, e.device_id, e.action, e.actor, e.detail, e.created_at "
            "FROM core.truck_arrival_hold_event e WHERE e.device_id = :d "
            "ORDER BY e.id DESC LIMIT :lim",
            {"d": device_id, "lim": limit})

    async def latest_hold_for(self, device_id: str) -> Optional[dict]:
        return await self._one(
            f"SELECT {_HOLD_COLS} FROM core.truck_arrival_hold WHERE device_id = :d "
            "ORDER BY held_at DESC LIMIT 1", {"d": device_id})


__all__ = ["YardCapacityRepository"]
