"""Air-quality persistence — raw-SQL repository over the shared async engine.

The ONLY layer that speaks SQL to ``core.air_quality_readings``. Mirrors
:mod:`services.traffic.repository`: reads on a plain ``connect()`` via the
``jnpa_shared.db`` helpers, writes through the committing helpers, no ORM.
Stateless apart from the DSN.

Every successful LIVE OpenAQ fetch is appended here (best-effort, by the
service) so the table doubles as (a) an audit trail of what the external API
actually said and (b) the DATABASE fallback rung — the last persisted reading
near a coordinate when both OpenAQ and the Redis cache are unavailable.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from jnpa_shared.db import execute_returning, fetch_all, fetch_one
from jnpa_shared.logging import get_logger

log = get_logger("services.air_quality.repository")

# Coordinates are matched at 2 decimal places (~1.1 km) so nearby requests for
# the port share one reading history rather than fragmenting per-decimal —
# same convention as services.traffic.repository.
_COORD_MATCH_SQL = ("round(CAST(latitude AS numeric), 2) = round(CAST(:lat AS numeric), 2) "
                    "AND round(CAST(longitude AS numeric), 2) = round(CAST(:lon AS numeric), 2)")

_READING_COLS = ("id, latitude, longitude, pm25, pm10, no2, so2, co, o3, "
                 "aq_status, source, payload, created_at")


class AirQualityRepository:
    """Raw-SQL persistence for core.air_quality_readings. Stateless apart from the DSN."""

    def __init__(self, dsn: Optional[str] = None) -> None:
        self._dsn = dsn

    async def insert_reading(
        self,
        *,
        latitude: float,
        longitude: float,
        pm25: Optional[float] = None,
        pm10: Optional[float] = None,
        no2: Optional[float] = None,
        so2: Optional[float] = None,
        co: Optional[float] = None,
        o3: Optional[float] = None,
        aq_status: Optional[str] = None,
        source: str = "OPENAQ",
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[int]:
        """Append one reading; returns the new id (committed)."""
        row = await execute_returning(
            """INSERT INTO core.air_quality_readings
                 (latitude, longitude, pm25, pm10, no2, so2, co, o3,
                  aq_status, source, payload)
               VALUES (:lat, :lon, :pm25, :pm10, :no2, :so2, :co, :o3,
                       :aq_status, :source, CAST(:payload AS jsonb))
               RETURNING id""",
            {
                "lat": latitude, "lon": longitude,
                "pm25": pm25, "pm10": pm10, "no2": no2,
                "so2": so2, "co": co, "o3": o3,
                "aq_status": aq_status, "source": source,
                "payload": json.dumps(payload or {}),
            },
            dsn=self._dsn,
        )
        return int(row["id"]) if row else None

    async def latest_reading(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        """The newest persisted reading for (roughly) this coordinate, or None."""
        row = await fetch_one(
            f"""SELECT {_READING_COLS}
                  FROM core.air_quality_readings
                 WHERE {_COORD_MATCH_SQL}
                 ORDER BY created_at DESC
                 LIMIT 1""",
            {"lat": latitude, "lon": longitude},
            dsn=self._dsn,
        )
        return _decode_payload(dict(row)) if row else None

    async def list_readings(
        self,
        *,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """Reading history, newest first (optionally scoped to one coordinate)."""
        where = f"WHERE {_COORD_MATCH_SQL}" if latitude is not None and longitude is not None else ""
        rows = await fetch_all(
            f"""SELECT {_READING_COLS}
                  FROM core.air_quality_readings
                 {where}
                 ORDER BY created_at DESC
                 LIMIT :limit OFFSET :offset""",
            {"lat": latitude, "lon": longitude, "limit": limit, "offset": offset},
            dsn=self._dsn,
        )
        return [_decode_payload(dict(r)) for r in rows]

    async def count_readings(
        self,
        *,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
    ) -> int:
        where = f"WHERE {_COORD_MATCH_SQL}" if latitude is not None and longitude is not None else ""
        row = await fetch_one(
            f"SELECT count(*) AS n FROM core.air_quality_readings {where}",
            {"lat": latitude, "lon": longitude},
            dsn=self._dsn,
        )
        return int(row["n"]) if row else 0


def _decode_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """jsonb may surface as str depending on driver codec setup — always a dict out."""
    payload = row.get("payload")
    if isinstance(payload, str):
        try:
            row["payload"] = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            row["payload"] = {}
    elif payload is None:
        row["payload"] = {}
    return row


__all__ = ["AirQualityRepository"]
