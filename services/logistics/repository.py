"""Logistics persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to the three logistics tables (migration 0109):

    core.logistics_event    — normalised ULIP logistics events (toll
                              crossings, container movements), deduped on
                              (ref_type, ref_id, event_type, event_ts,
                              location) — null-safe via COALESCE
    core.logistics_tracking — one snapshot row per tracked reference
                              (vehicle / container), upserted on every
                              successful LIVE fetch
    core.ulip_api_audit     — one row per outbound ULIP API call (success or
                              failure) — the compliance audit trail

Mirrors :mod:`services.traffic.repository`: reads on a plain ``connect()`` via
the ``jnpa_shared.db`` helpers, writes through the committing helpers, no ORM.
Stateless apart from the DSN.

The event/tracking tables double as the DATABASE fallback rung — the last
persisted answer for a reference when both ULIP and the Redis cache are
unavailable (LIVE -> CACHED -> DATABASE -> FALLBACK).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from gateway.datewindow import window_cond  # GAP-DATE-01

from jnpa_shared.db import execute_returning, fetch_all, fetch_one
from jnpa_shared.logging import get_logger

log = get_logger("services.logistics.repository")

_EVENT_COLS = ("id, ref_type, ref_id, event_type, event_ts, location, "
               "latitude, longitude, source, source_api, detail, created_at")

_TRACKING_COLS = ("id, ref_type, ref_id, status, last_event, last_location, "
                  "last_event_ts, event_count, source, payload, "
                  "created_at, updated_at")


def _parse_ts(value: Any) -> Any:
    """asyncpg needs a real datetime for a timestamptz bind. Convert an ISO
    string to datetime; pass datetimes/None through."""
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


class LogisticsRepository:
    """Raw-SQL persistence for the core.logistics_* + core.ulip_api_audit
    tables. Stateless apart from the DSN."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    # ------------------------------------------------------------------ events
    async def insert_events(self, events: List[Dict[str, Any]]) -> int:
        """Append normalised events, skipping rows already present
        (ON CONFLICT on the dedup key — re-fetching a reference must never
        duplicate its history). Returns the number of NEW rows."""
        inserted = 0
        for event in events:
            row = await execute_returning(
                """INSERT INTO core.logistics_event
                     (ref_type, ref_id, event_type, event_ts, location,
                      latitude, longitude, source, source_api, detail)
                   VALUES (:ref_type, :ref_id, :event_type,
                           CAST(:event_ts AS timestamptz), :location,
                           :latitude, :longitude, :source, :source_api,
                           CAST(:detail AS jsonb))
                   ON CONFLICT (ref_type, ref_id, event_type,
                                COALESCE(event_ts, 'epoch'::timestamptz),
                                COALESCE(location, ''))
                   DO NOTHING
                   RETURNING id""",
                {
                    "ref_type": event.get("ref_type"),
                    "ref_id": event.get("ref_id"),
                    "event_type": event.get("event_type"),
                    "event_ts": _parse_ts(event.get("event_ts")),
                    "location": event.get("location"),
                    "latitude": event.get("latitude"),
                    "longitude": event.get("longitude"),
                    "source": event.get("source", "ULIP"),
                    "source_api": event.get("source_api"),
                    "detail": json.dumps(event.get("detail") or {}),
                },
                dsn=self._dsn,
            )
            if row is not None:
                inserted += 1
        return inserted

    async def list_events(
        self,
        *,
        ref_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
                          window: Any = None,
                          date_col: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Event history, newest first (optionally scoped by reference/type)."""
        clauses, params = [], {"limit": limit, "offset": offset}
        if ref_id:
            clauses.append("ref_id = :ref_id")
            params["ref_id"] = ref_id.strip().upper()
        if event_type:
            clauses.append("event_type = :event_type")
            params["event_type"] = event_type
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await fetch_all(
            f"""SELECT {_EVENT_COLS}
                  FROM core.logistics_event
                 {where}
                 ORDER BY event_ts DESC NULLS LAST, id DESC
                 LIMIT :limit OFFSET :offset""",
            params,
            dsn=self._dsn,
        )
        # GAP-DATE-01. `date_col` is named by the CALLER — this method
        # serves several tables, and a guessed column filters the wrong
        # one, returning a plausible answer rather than an error.
        _wcond = window_cond(window, date_col, params) if date_col else None
        if _wcond:
            clauses.append(_wcond)

        return [_decode_row(dict(r), "detail") for r in rows]

    async def count_events(
        self,
        *,
        ref_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> int:
        clauses, params = [], {}
        if ref_id:
            clauses.append("ref_id = :ref_id")
            params["ref_id"] = ref_id.strip().upper()
        if event_type:
            clauses.append("event_type = :event_type")
            params["event_type"] = event_type
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = await fetch_one(
            f"SELECT count(*) AS n FROM core.logistics_event {where}",
            params, dsn=self._dsn)
        return int(row["n"]) if row else 0

    # ---------------------------------------------------------------- tracking
    async def upsert_tracking(
        self,
        *,
        ref_type: str,
        ref_id: str,
        status: str,
        last_event: Optional[str] = None,
        last_location: Optional[str] = None,
        last_event_ts: Optional[str] = None,
        event_count: int = 0,
        source: str = "ULIP",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Insert-or-refresh the snapshot row for one tracked reference."""
        row = await execute_returning(
            """INSERT INTO core.logistics_tracking
                 (ref_type, ref_id, status, last_event, last_location,
                  last_event_ts, event_count, source, payload)
               VALUES (:ref_type, :ref_id, :status, :last_event, :last_location,
                       CAST(:last_event_ts AS timestamptz), :event_count,
                       :source, CAST(:payload AS jsonb))
               ON CONFLICT (ref_type, ref_id) DO UPDATE SET
                 status = EXCLUDED.status,
                 last_event = EXCLUDED.last_event,
                 last_location = EXCLUDED.last_location,
                 last_event_ts = EXCLUDED.last_event_ts,
                 event_count = EXCLUDED.event_count,
                 source = EXCLUDED.source,
                 payload = EXCLUDED.payload,
                 updated_at = now()
               RETURNING id""",
            {
                "ref_type": ref_type, "ref_id": ref_id.strip().upper(),
                "status": status, "last_event": last_event,
                "last_location": last_location,
                "last_event_ts": _parse_ts(last_event_ts),
                "event_count": event_count, "source": source,
                "payload": json.dumps(payload or {}),
            },
            dsn=self._dsn,
        )
        return int(row["id"]) if row else None

    async def get_tracking(self, ref_id: str) -> Optional[Dict[str, Any]]:
        """The persisted snapshot for one reference, or None."""
        row = await fetch_one(
            f"""SELECT {_TRACKING_COLS}
                  FROM core.logistics_tracking
                 WHERE ref_id = :ref_id
                 ORDER BY updated_at DESC
                 LIMIT 1""",
            {"ref_id": ref_id.strip().upper()},
            dsn=self._dsn,
        )
        return _decode_row(dict(row), "payload") if row else None

    async def list_tracking(self, *, limit: int = 50) -> List[Dict[str, Any]]:
        """The most recently refreshed snapshots (for the summary surface)."""
        rows = await fetch_all(
            f"""SELECT {_TRACKING_COLS}
                  FROM core.logistics_tracking
                 ORDER BY updated_at DESC
                 LIMIT :limit""",
            {"limit": limit},
            dsn=self._dsn,
        )
        return [_decode_row(dict(r), "payload") for r in rows]

    # ----------------------------------------------------------------- summary
    async def summary(self, *, window_h: int = 24) -> Dict[str, Any]:
        """Aggregates over the persisted events for the summary surface."""
        row = await fetch_one(
            """SELECT count(*) AS event_count,
                      count(DISTINCT ref_id) FILTER (WHERE ref_type = 'VEHICLE')
                        AS vehicle_count,
                      count(DISTINCT ref_id) FILTER (WHERE ref_type = 'CONTAINER')
                        AS container_count,
                      max(event_ts) AS last_event_ts
                 FROM core.logistics_event
                WHERE created_at >= now() - make_interval(hours => :window_h)""",
            {"window_h": window_h},
            dsn=self._dsn,
        )
        by_type = await fetch_all(
            """SELECT event_type, count(*) AS n
                 FROM core.logistics_event
                WHERE created_at >= now() - make_interval(hours => :window_h)
                GROUP BY event_type
                ORDER BY n DESC""",
            {"window_h": window_h},
            dsn=self._dsn,
        )
        out = dict(row) if row else {"event_count": 0, "vehicle_count": 0,
                                     "container_count": 0, "last_event_ts": None}
        out["events_by_type"] = {r["event_type"]: int(r["n"]) for r in by_type}
        return out

    # ------------------------------------------------------------------- audit
    async def insert_audit(
        self,
        *,
        api_name: str,
        ref_type: Optional[str] = None,
        ref_id: Optional[str] = None,
        ok: bool = False,
        http_status: Optional[int] = None,
        latency_ms: Optional[float] = None,
        error: Optional[str] = None,
        response: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Append one ULIP call audit row (success or failure; committed)."""
        row = await execute_returning(
            """INSERT INTO core.ulip_api_audit
                 (api_name, ref_type, ref_id, ok, http_status, latency_ms,
                  error, response)
               VALUES (:api_name, :ref_type, :ref_id, :ok, :http_status,
                       :latency_ms, :error, CAST(:response AS jsonb))
               RETURNING id""",
            {
                "api_name": api_name, "ref_type": ref_type,
                "ref_id": ref_id.strip().upper() if ref_id else None,
                "ok": ok, "http_status": http_status,
                "latency_ms": latency_ms, "error": error,
                "response": json.dumps(response or {}),
            },
            dsn=self._dsn,
        )
        return int(row["id"]) if row else None

    async def last_audit(self) -> Optional[Dict[str, Any]]:
        """The newest audit row (drives the freshness/posture metadata)."""
        row = await fetch_one(
            """SELECT id, api_name, ref_type, ref_id, ok, http_status,
                      latency_ms, error, created_at
                 FROM core.ulip_api_audit
                ORDER BY created_at DESC
                LIMIT 1""",
            dsn=self._dsn,
        )
        return dict(row) if row else None


def _decode_row(row: Dict[str, Any], json_col: str) -> Dict[str, Any]:
    """jsonb may surface as str depending on driver codec setup — always a
    dict out; datetimes out as ISO strings (JSON-safe)."""
    value = row.get(json_col)
    if isinstance(value, str):
        try:
            row[json_col] = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            row[json_col] = {}
    elif value is None:
        row[json_col] = {}
    for key, val in list(row.items()):
        if isinstance(val, datetime):
            row[key] = val.isoformat()
    return row


__all__ = ["LogisticsRepository"]
