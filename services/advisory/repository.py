"""Driver-advisory persistence — raw-SQL repository over the shared async engine.

Backs ``core.reroute_advisory`` (migration 0115). One row per device holding the
most recent re-route advisory and its ACK state.

Every method is BEST-EFFORT by construction: a re-route must still reach the
driver over WebSocket/WebPush/FCM when RDS is briefly unavailable, so failures
are logged and swallowed and the caller falls back to the in-memory cache. The
push itself is never blocked on a database write.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from sqlalchemy import text

from jnpa_shared.db import get_engine
from jnpa_shared.logging import get_logger

log = get_logger("services.advisory.repository")


class AdvisoryRepository:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def save(self, device_id: str, advisory: Mapping[str, Any]) -> bool:
        """Upsert the latest advisory for ``device_id``. Returns True when stored.

        Re-dispatching to the same device replaces the row and clears the previous
        ACK — a new advisory has not been acknowledged yet.
        """
        if not self._dsn or not device_id:
            return False
        sql = """
            INSERT INTO core.reroute_advisory
                (device_id, plate, from_gate, to_gate, reason, advisory,
                 ack_state, acked_at, dispatched_at, updated_at)
            VALUES (:d, :plate, :fg, :tg, :reason, CAST(:adv AS jsonb),
                    NULL, NULL, now(), now())
            ON CONFLICT (device_id) DO UPDATE SET
                plate         = EXCLUDED.plate,
                from_gate     = EXCLUDED.from_gate,
                to_gate       = EXCLUDED.to_gate,
                reason        = EXCLUDED.reason,
                advisory      = EXCLUDED.advisory,
                ack_state     = NULL,
                acked_at      = NULL,
                dispatched_at = now(),
                updated_at    = now()
        """
        params = {
            "d": device_id,
            "plate": advisory.get("plate"),
            "fg": advisory.get("from_gate"),
            "tg": advisory.get("gate_id") or advisory.get("dest"),
            "reason": advisory.get("reason"),
            "adv": json.dumps(dict(advisory), default=str),
        }
        try:
            async with get_engine(self._dsn).begin() as conn:
                await conn.execute(text(sql), params)
            return True
        except Exception as exc:  # noqa: BLE001 — never fail a driver push on RDS
            log.warning("advisory.save_failed", device_id=device_id, error=str(exc))
            return False

    async def ack(self, device_id: str, state_val: str) -> bool:
        """Record the driver's ACK/DECLINE against the stored advisory."""
        if not self._dsn or not device_id:
            return False
        if state_val not in ("ACK", "DECLINE"):
            state_val = "ACK"
        try:
            async with get_engine(self._dsn).begin() as conn:
                res = await conn.execute(
                    text("UPDATE core.reroute_advisory "
                         "SET ack_state = :s, acked_at = now(), updated_at = now() "
                         "WHERE device_id = :d"),
                    {"s": state_val, "d": device_id})
            return bool(res.rowcount)
        except Exception as exc:  # noqa: BLE001
            log.warning("advisory.ack_failed", device_id=device_id, error=str(exc))
            return False

    async def latest(self, device_id: str) -> Optional[dict]:
        """The stored advisory for ``device_id`` (with its ACK state), or None."""
        if not self._dsn or not device_id:
            return None
        try:
            async with get_engine(self._dsn).connect() as conn:
                row = (await conn.execute(
                    text("SELECT advisory, ack_state, acked_at, dispatched_at "
                         "FROM core.reroute_advisory WHERE device_id = :d"),
                    {"d": device_id})).mappings().first()
        except Exception as exc:  # noqa: BLE001
            log.warning("advisory.latest_failed", device_id=device_id, error=str(exc))
            return None
        if row is None:
            return None
        adv = dict(row["advisory"] or {})
        adv["ack_state"] = row["ack_state"]
        adv["acked_at"] = row["acked_at"].isoformat() if row["acked_at"] else None
        adv["dispatched_at"] = (row["dispatched_at"].isoformat()
                                if row["dispatched_at"] else None)
        return adv


__all__ = ["AdvisoryRepository"]
