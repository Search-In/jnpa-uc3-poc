"""Cross-twin deferred-arrival persistence — raw-SQL over the shared async engine.

Backs ``core.deferred_arrival_window`` (migration 0115).

Idempotency is the point: UC-II may redeliver the same ``correlation_id`` after a
consumer restart, a partition rebalance, or a re-run of its S2 scenario. The
upsert below keeps ONE row per correlation_id and preserves the ``booked``
counter across redeliveries, which is exactly the semantics
``gateway.tas_mock.apply_deferred_window`` already implements in memory.

Best-effort like the rest of the cross-twin path: a broker event must still meter
the TAS slot book when RDS is briefly unavailable, so failures are logged and
swallowed.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.crosstwin.repository")


def _as_dt(value: Any) -> Optional[datetime]:
    """Coerce a window timestamp to a tz-aware ``datetime`` for asyncpg.

    ``tas_mock._window_dict`` serialises its timestamps to ISO strings for the
    HTTP/WS surfaces, but asyncpg binds parameters by Python type and rejects a
    ``str`` for a timestamptz column (``expected a datetime.date or
    datetime.datetime instance``) — a CAST in the SQL does not help, because the
    driver never gets far enough to see it. Accepting both shapes keeps the
    in-memory book free to hand over whichever it has.
    """
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # fromisoformat handles the "+00:00" offset _window_dict emits.
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise TypeError(f"unsupported timestamp type: {type(value).__name__}")


class DeferredArrivalRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def upsert(self, window: Mapping[str, Any], *, transport: str = "KAFKA") -> bool:
        """Persist one applied window. ``window`` is ``tas_mock._window_dict``.

        ``booked`` uses GREATEST(existing, incoming) so a redelivery can never
        reset a counter that live bookings have already advanced.
        """
        if not self._dsn:
            return False
        cid = window.get("correlation_id")
        if not cid:
            return False
        sql = """
            INSERT INTO core.deferred_arrival_window
                (correlation_id, gate_id, window_start, window_end, window_min,
                 slot_cap, booked, applied_slots, source, transport,
                 received_at, updated_at)
            VALUES (:cid, :gate, :ws, :we,
                    :wmin, :cap, :booked, CAST(:slots AS jsonb), :src, :tr,
                    now(), now())
            ON CONFLICT (correlation_id) DO UPDATE SET
                gate_id       = EXCLUDED.gate_id,
                window_start  = EXCLUDED.window_start,
                window_end    = EXCLUDED.window_end,
                window_min    = EXCLUDED.window_min,
                slot_cap      = EXCLUDED.slot_cap,
                booked        = GREATEST(core.deferred_arrival_window.booked,
                                         EXCLUDED.booked),
                applied_slots = EXCLUDED.applied_slots,
                source        = EXCLUDED.source,
                transport     = EXCLUDED.transport,
                updated_at    = now()
        """
        try:
            ws, we = _as_dt(window.get("window_start")), _as_dt(window.get("window_end"))
        except (TypeError, ValueError) as exc:
            log.warning("crosstwin.window_bad_timestamps", correlation_id=cid,
                        error=str(exc))
            return False
        params = {
            "cid": cid,
            "gate": window.get("gate_id"),
            "ws": ws,
            "we": we,
            "wmin": int(window.get("window_min") or 0),
            "cap": int(window.get("slot_cap") or 0),
            "booked": int(window.get("booked") or 0),
            "slots": json.dumps(list(window.get("applied_slots") or [])),
            "src": window.get("source"),
            "tr": transport if transport in ("KAFKA", "HTTP") else "KAFKA",
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(sql), params)
            return True
        except Exception as exc:  # noqa: BLE001 — metering must not depend on RDS
            log.warning("crosstwin.window_persist_failed", correlation_id=cid,
                        error=str(exc))
            return False

    async def bump_booked(self, correlation_id: str) -> None:
        """Increment the persisted booking counter after an accepted booking."""
        if not self._dsn or not correlation_id:
            return
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(
                    text("UPDATE core.deferred_arrival_window "
                         "SET booked = booked + 1, updated_at = now() "
                         "WHERE correlation_id = :c"), {"c": correlation_id})
        except Exception as exc:  # noqa: BLE001
            log.warning("crosstwin.bump_failed", correlation_id=correlation_id,
                        error=str(exc))

    async def recent(self, limit: int = 32) -> list[dict]:
        """Persisted windows, newest first — used to replay state on gateway boot."""
        if not self._dsn:
            return []
        try:
            async with get_engine(self._dsn).connect() as conn:
                rows = (await conn.execute(
                    text("SELECT correlation_id, gate_id, window_start, window_end, "
                         "       window_min, slot_cap, booked, applied_slots, source, "
                         "       transport, received_at "
                         "FROM core.deferred_arrival_window "
                         "ORDER BY received_at DESC LIMIT :n"),
                    {"n": int(limit)})).mappings().all()
        except Exception as exc:  # noqa: BLE001
            log.warning("crosstwin.recent_failed", error=str(exc))
            return []
        out: list[dict] = []
        for r in rows:
            out.append({
                "correlation_id": r["correlation_id"],
                "gate_id": r["gate_id"],
                "window_start": r["window_start"].isoformat() if r["window_start"] else None,
                "window_end": r["window_end"].isoformat() if r["window_end"] else None,
                "window_min": r["window_min"],
                "slot_cap": r["slot_cap"],
                "booked": r["booked"],
                "applied_slots": list(r["applied_slots"] or []),
                "source": r["source"],
                "transport": r["transport"],
                "received_at": r["received_at"].isoformat() if r["received_at"] else None,
            })
        return out


__all__ = ["DeferredArrivalRepository"]
