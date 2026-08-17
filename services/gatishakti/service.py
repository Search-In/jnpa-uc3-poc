"""GatiShakti reference data (ULIP GATISHAKTI/01..04) with the standard chain.

    LIVE      -> fresh ULIP fetch (toll plazas / road network / road points)
    CACHED    -> last good answer from Redis
    DATABASE  -> the persisted core.gs_* reference rows
    FALLBACK  -> an explicitly-empty answer, clearly tagged — NO fabricated
                 road or plaza data

Deliberately unlike the weather/traffic SYNTHETIC rungs, and for the same
reason :mod:`services.logistics.service` refuses to invent shipments: a
made-up toll plaza or road point would be read as real infrastructure — it
would place a gantry that does not exist on an operational map. An empty,
tagged answer is always the better failure.

Unlike the logistics surfaces this is slow-moving MASTER data, so the DATABASE
rung is not a degraded fallback in normal operation — it is the expected
serving path. LIVE is what refreshes it, typically on a manual/scheduled
refresh rather than per request, which is why :meth:`GatiShaktiService.refresh`
is separate from the read methods.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from integrations.ulip import UlipClient, UlipError
from integrations.ulip.schemas import normalize_road_network, normalize_toll_plazas
from jnpa_shared import redis_io
from jnpa_shared.logging import get_logger

from .repository import GatiShaktiRepository

log = get_logger("services.gatishakti.service")

CACHE_PREFIX = "jnpa:cache:gatishakti"
DEFAULT_CACHE_TTL_S = 3600  # reference data — an hour is aggressive already

PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_DATABASE = "DATABASE"
PATH_FALLBACK = "FALLBACK"
_PATH_SOURCE = {PATH_LIVE: "ULIP", PATH_CACHED: "ULIP_CACHE",
                PATH_DATABASE: "ULIP_DB", PATH_FALLBACK: "NONE"}

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# LGD state codes for the states the JNPA corridor actually touches. GatiShakti
# keys /02, /03 and /04 by this code, never by name.
STATE_MAHARASHTRA = "27"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _envelope(rows: List[Dict[str, Any]], path: str) -> Dict[str, Any]:
    """One answer shape for every read, so the router never branches on rung."""
    return {
        "rows": rows,
        "count": len(rows),
        "data_available": bool(rows),
        "path": path,
        "source": _PATH_SOURCE[path],
        "status": (STATUS_LIVE if path == PATH_LIVE
                   else STATUS_OFFLINE if path == PATH_FALLBACK
                   else STATUS_DEGRADED),
        "as_of": _now_iso(),
    }


def _cache_key(api: str, state_id: str) -> str:
    """One spelling of the cache key, shared by the writer and every reader.

    Writer and reader used to spell it independently (``:toll_plaza:`` on the
    way in, ``:plazas:`` on the way out), so the CACHED rung never hit once —
    the miss was invisible because the DATABASE rung answers first in normal
    operation and only a cold table would have exposed it.
    """
    return f"{CACHE_PREFIX}:{api}:{state_id}"


async def _cache_put(key: str, value: List[Dict[str, Any]], ttl: int) -> None:
    try:
        await redis_io.cache_set(key, {"cached_at": _now_iso(), "value": value},
                                 ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("gatishakti_cache_put_failed", key=key, error=str(exc))


async def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    try:
        wrapped = await redis_io.cache_get(key)
    except Exception as exc:  # pragma: no cover
        log.warning("gatishakti_cache_get_failed", key=key, error=str(exc))
        return None
    if isinstance(wrapped, dict) and isinstance(wrapped.get("value"), list):
        return wrapped["value"]
    return None


class GatiShaktiService:
    """Fetch, persist and serve GatiShakti reference data."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[UlipClient] = None,
        repository: Optional[GatiShaktiRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
    ) -> None:
        self._client = client or UlipClient()
        self._repo = repository or GatiShaktiRepository(dsn)
        self.cache_ttl_s = cache_ttl_s

    @property
    def configured(self) -> bool:
        return self._client.configured

    # ------------------------------------------------------------------ reads
    async def toll_plazas(self, *, state_id: str = STATE_MAHARASHTRA,
                          limit: int = 500) -> Dict[str, Any]:
        """NHAI toll plazas for a state. DATABASE-first: this is master data
        refreshed by :meth:`refresh`, not something to re-fetch per request."""
        key = _cache_key("GATISHAKTI/04", state_id)
        rows = await self._repo.list_toll_plazas(state_id=state_id, limit=limit)
        if rows:
            return _envelope(rows, PATH_DATABASE)
        cached = await _cache_get(key)
        if cached:
            return _envelope(cached[:limit], PATH_CACHED)
        return _envelope([], PATH_FALLBACK)

    async def roads(self, *, state_id: Optional[str] = None,
                    nh_no: Optional[str] = None,
                    source_api: Optional[str] = None,
                    limit: int = 500) -> Dict[str, Any]:
        rows = await self._repo.list_road_segments(state_id=state_id, nh_no=nh_no,
                                                   source_api=source_api,
                                                   limit=limit)
        return _envelope(rows, PATH_DATABASE if rows else PATH_FALLBACK)

    async def road_points(self, *, state_id: str = STATE_MAHARASHTRA,
                          limit: int = 1000) -> Dict[str, Any]:
        rows = await self._repo.list_road_points(state_id=state_id, limit=limit)
        return _envelope(rows, PATH_DATABASE if rows else PATH_FALLBACK)

    async def nh_numbers(self, *, limit: int = 200) -> Dict[str, Any]:
        """Which NH numbers GATISHAKTI/01 has been refreshed for."""
        rows = await self._repo.list_nh_numbers(limit=limit)
        return _envelope(rows, PATH_DATABASE if rows else PATH_FALLBACK)

    # --------------------------------------------------------------- refresh
    async def refresh(self, *, state_id: str = STATE_MAHARASHTRA,
                      nh_no: Optional[str] = None,
                      nh_nos: Optional[List[str]] = None) -> Dict[str, Any]:
        """Pull the reference set from ULIP and persist it.

        Each API is fetched independently and a failure in one never aborts the
        others — a GatiShakti outage on the road-points endpoint must not cost
        us a fresh toll-plaza registry. Per-API outcomes are reported so the
        caller can see exactly what refreshed and what did not.

        GATISHAKTI/01 is keyed by NH number rather than by state, so it is the
        one part of the set that cannot be refreshed by naming a state: every
        highway wanted has to be asked for. ``nh_nos`` takes a list so seeding
        a corridor is one call instead of one call per highway; ``nh_no``
        remains for the single-highway case.
        """
        wanted_nh: List[str] = []
        for candidate in ([nh_no] if nh_no else []) + list(nh_nos or []):
            text = str(candidate).strip().upper()
            if text and text not in wanted_nh:
                wanted_nh.append(text)
        outcome: Dict[str, Any] = {"state_id": state_id, "nh_no": nh_no,
                                   "nh_nos": wanted_nh, "apis": {},
                                   "written": 0}
        # (kind, api, report_key, fetch, normalise). `api` is what gets stamped
        # onto every row's source_api and must stay the bare API path so the
        # read filter can match it; `report_key` is what the caller sees, and
        # for /01 it carries the highway so a per-highway failure is legible.
        plans = [
            ("toll_plaza", "GATISHAKTI/04", "GATISHAKTI/04",
             lambda: self._client.fetch_toll_plazas(state_id),
             lambda env: normalize_toll_plazas(env, state_id)),
            ("road_point", "GATISHAKTI/03", "GATISHAKTI/03",
             lambda: self._client.fetch_state_road_points(state_id),
             lambda env: normalize_road_network(env, state_id=state_id)),
            ("road_segment", "GATISHAKTI/02", "GATISHAKTI/02",
             lambda: self._client.fetch_state_roads(state_id),
             lambda env: normalize_road_network(env, state_id=state_id)),
        ]
        for highway in wanted_nh:
            # Bind `highway` per iteration — a closure over the loop variable
            # would fetch the LAST highway once per entry.
            plans.append(
                ("road_segment", "GATISHAKTI/01", f"GATISHAKTI/01:{highway}",
                 lambda h=highway: self._client.fetch_nh_road(h),
                 lambda env, h=highway: normalize_road_network(env, nh_no=h)))

        for kind, api, report_key, fetch, normalise in plans:
            try:
                envelope = await fetch()
            except UlipError as exc:
                outcome["apis"][report_key] = {"status": "FAILED",
                                               "error": type(exc).__name__}
                log.warning("gatishakti_refresh_failed", api=report_key,
                            error=type(exc).__name__)
                continue
            rows = normalise(envelope)
            for row in rows:
                row.setdefault("source_api", api)
            written = await self._repo.upsert(kind, rows)
            outcome["apis"][report_key] = {"status": "OK", "rows": len(rows),
                                           "written": written}
            outcome["written"] += written
            if rows:
                # Keyed by report_key, not by `kind`: /01 and /02 are both
                # "road_segment", so a kind-keyed entry let each refreshed
                # highway overwrite the state's depot cache — and the plaza
                # writer used a name the reader never looked up, leaving the
                # CACHED rung permanently dead. See _cache_key.
                await _cache_put(_cache_key(report_key, state_id), rows,
                                 self.cache_ttl_s)
        log.info("gatishakti_refresh", **{k: v for k, v in outcome.items()
                                          if k != "apis"})
        return outcome

    # ---------------------------------------------------------------- health
    async def health(self) -> Dict[str, Any]:
        """Posture for /api/gatishakti/health — never raises, never leaks a
        credential."""
        counts = await self._repo.counts()
        return {
            "module": "gatishakti",
            "configured": self.configured,
            "auth_mode": self._client.auth_mode,
            "apis": {
                "nh_road": self._client.api_path("GS_NH_ROAD"),
                "state_roads": self._client.api_path("GS_STATE_ROADS"),
                "road_points": self._client.api_path("GS_ROAD_POINTS"),
                "toll_plazas": self._client.api_path("GS_TOLL_PLAZAS"),
            },
            "rows": counts,
            "seeded": any(counts.values()),
            "status": STATUS_LIVE if any(counts.values()) else STATUS_OFFLINE,
        }


__all__ = ["GatiShaktiService", "GatiShaktiRepository", "STATE_MAHARASHTRA",
           "PATH_LIVE", "PATH_CACHED", "PATH_DATABASE", "PATH_FALLBACK"]
