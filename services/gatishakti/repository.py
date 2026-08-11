"""Raw-SQL persistence for the core.gs_* GatiShakti reference tables.

Same shape as :mod:`services.logistics.repository`: no ORM, explicit SQL, every
write idempotent so a re-fetch refreshes rather than duplicates. Reference data
is slow-moving master data, so the write path is an UPSERT keyed on the natural
identity (see migration 0134) rather than an append-only insert.

Every method degrades to an empty/zero answer when the DB is unreachable — the
service above treats that as "the DATABASE rung has nothing", which is exactly
what it means, and drops to FALLBACK.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from jnpa_shared.logging import get_logger

log = get_logger("services.gatishakti.repository")

_UPSERT_PLAZA = """
INSERT INTO core.gs_toll_plaza
    (state_id, name, nh_no, latitude, longitude, source_api, detail, fetched_at)
VALUES
    (:state_id, :name, :nh_no, :latitude, :longitude, :source_api,
     CAST(:detail AS jsonb), now())
ON CONFLICT (state_id, name, COALESCE(nh_no, '')) DO UPDATE SET
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    detail = EXCLUDED.detail,
    fetched_at = now()
"""

_UPSERT_SEGMENT = """
INSERT INTO core.gs_road_segment
    (state_id, nh_no, name, latitude, longitude, source_api, detail, fetched_at)
VALUES
    (:state_id, :nh_no, :name, :latitude, :longitude, :source_api,
     CAST(:detail AS jsonb), now())
ON CONFLICT (COALESCE(state_id, ''), COALESCE(nh_no, ''), COALESCE(name, ''))
DO UPDATE SET
    latitude = EXCLUDED.latitude,
    longitude = EXCLUDED.longitude,
    detail = EXCLUDED.detail,
    fetched_at = now()
"""

_UPSERT_POINT = """
INSERT INTO core.gs_road_point
    (state_id, name, latitude, longitude, source_api, detail, fetched_at)
VALUES
    (:state_id, :name, :latitude, :longitude, :source_api,
     CAST(:detail AS jsonb), now())
ON CONFLICT (state_id, COALESCE(name, ''), COALESCE(latitude, 0),
             COALESCE(longitude, 0))
DO UPDATE SET
    detail = EXCLUDED.detail,
    fetched_at = now()
"""

_TABLES = {
    "toll_plaza": ("core.gs_toll_plaza", _UPSERT_PLAZA),
    "road_segment": ("core.gs_road_segment", _UPSERT_SEGMENT),
    "road_point": ("core.gs_road_point", _UPSERT_POINT),
}


def _j(value: Any) -> str:
    try:
        return json.dumps(value if value is not None else {}, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


class GatiShaktiRepository:
    """Persistence for the three GatiShakti reference tables."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def upsert(self, kind: str, rows: List[Dict[str, Any]]) -> int:
        """UPSERT reference rows; returns how many were written.

        ``kind`` is one of ``toll_plaza`` / ``road_segment`` / ``road_point``.
        Rows are written one statement per row rather than as a batch: the
        volumes are small (hundreds per state, refreshed rarely) and per-row
        writes let one malformed row be skipped without losing the pass.
        """
        if not self._dsn or not rows:
            return 0
        table_sql = _TABLES.get(kind)
        if table_sql is None:
            raise ValueError(f"unknown GatiShakti table kind {kind!r}")
        _, sql = table_sql
        from jnpa_shared.db import execute

        written = 0
        for row in rows:
            params = {
                "state_id": row.get("state_id"),
                "name": row.get("name"),
                "latitude": row.get("latitude"),
                "longitude": row.get("longitude"),
                "source_api": row.get("source_api") or "GATISHAKTI",
                "detail": _j(row.get("detail")),
            }
            # gs_road_point has no nh_no column; binding a parameter the
            # statement does not name is an error, not a no-op.
            if kind != "road_point":
                params["nh_no"] = row.get("nh_no")
            try:
                await execute(sql, params, dsn=self._dsn)
                written += 1
            except Exception as exc:  # noqa: BLE001 — one bad row, not the pass
                log.warning("gatishakti_upsert_failed", kind=kind,
                            name=row.get("name"), error=str(exc))
        return written

    async def list_toll_plazas(self, *, state_id: Optional[str] = None,
                               limit: int = 500) -> List[Dict[str, Any]]:
        return await self._select(
            """
            SELECT state_id, name, nh_no, latitude, longitude, fetched_at
            FROM core.gs_toll_plaza
            WHERE (CAST(:state_id AS text) IS NULL OR state_id = CAST(:state_id AS text))
            ORDER BY name
            LIMIT :limit
            """,
            {"state_id": state_id, "limit": _cap(limit)},
        )

    async def list_road_segments(self, *, state_id: Optional[str] = None,
                                 nh_no: Optional[str] = None,
                                 limit: int = 500) -> List[Dict[str, Any]]:
        return await self._select(
            """
            SELECT state_id, nh_no, name, latitude, longitude, fetched_at
            FROM core.gs_road_segment
            WHERE (CAST(:state_id AS text) IS NULL OR state_id = CAST(:state_id AS text))
              AND (CAST(:nh_no AS text) IS NULL OR nh_no = CAST(:nh_no AS text))
            ORDER BY name
            LIMIT :limit
            """,
            {"state_id": state_id, "nh_no": nh_no, "limit": _cap(limit)},
        )

    async def list_road_points(self, *, state_id: Optional[str] = None,
                               limit: int = 1000) -> List[Dict[str, Any]]:
        return await self._select(
            """
            SELECT state_id, name, latitude, longitude, fetched_at
            FROM core.gs_road_point
            WHERE (CAST(:state_id AS text) IS NULL OR state_id = CAST(:state_id AS text))
            ORDER BY name
            LIMIT :limit
            """,
            {"state_id": state_id, "limit": _cap(limit)},
        )

    async def counts(self) -> Dict[str, int]:
        """Row counts per table — the health surface's "is it seeded?" answer."""
        out: Dict[str, int] = {}
        for kind, (table, _sql) in _TABLES.items():
            rows = await self._select(f"SELECT count(*) AS n FROM {table}", {})
            out[kind] = int(rows[0]["n"]) if rows else 0
        return out

    async def _select(self, sql: str, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        if not self._dsn:
            return []
        from jnpa_shared.db import fetch_all

        try:
            rows = await fetch_all(sql, params, dsn=self._dsn)
        except Exception as exc:  # noqa: BLE001 — DB down == rung is empty
            # WARNING, not debug: an empty rung is indistinguishable from "this
            # state has no toll plazas" on the wire, so a query that fails
            # silently reads as legitimately-absent data. A malformed statement
            # hid here for a whole release — every list endpoint returned
            # FALLBACK/empty while the rows were sitting in the table.
            log.warning("gatishakti_select_failed", error=str(exc))
            return []
        out: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            fetched = record.get("fetched_at")
            if fetched is not None and hasattr(fetched, "isoformat"):
                record["fetched_at"] = fetched.isoformat()
            out.append(record)
        return out


def _cap(limit: int) -> int:
    return max(1, min(int(limit), 5000))


__all__ = ["GatiShaktiRepository"]
