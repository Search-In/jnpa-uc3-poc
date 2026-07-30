"""Logistics service — ULIP vehicle/container intelligence with graceful
fallback.

The single entry point the /api/logistics/* routers call. Thin over
:class:`integrations.ulip.UlipClient` (HTTP) and
:class:`services.logistics.repository.LogisticsRepository` (persistence), in
the same mould as services.traffic.service: stateless apart from config.

Fallback chain for a tracking lookup (mirroring the traffic service's
vocabulary — a provider outage must NEVER break the API):

    LIVE      -> fresh ULIP fetch (FASTAG for a vehicle number, LDB for an
                 ISO-6346 container number), normalised + persisted
    CACHED    -> last good answer from Redis
                 (key jnpa:cache:ulip:tracking:{ref})
    DATABASE  -> the persisted core.logistics_tracking snapshot + event
                 history (the audit trail doubles as the third rung)
    FALLBACK  -> an explicitly-empty answer, clearly tagged — NO fabricated
                 shipment data, ever (deliberately unlike the SYNTHETIC rungs
                 of the weather/traffic surfaces: a made-up toll crossing or
                 container movement would be operationally misleading)

Response contract (source metadata always attached):
    status  LIVE      — fresh from ULIP
            DEGRADED  — served from the cache or database rung
            OFFLINE   — nothing real available (empty fallback)
    source  ULIP | ULIP_CACHE | ULIP_DB | NONE  (rung that answered)

Every outbound ULIP call — success OR failure — is audited to
core.ulip_api_audit (best-effort; an infra blip never fails the request).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from jnpa_shared import redis_io
from jnpa_shared.logging import get_logger

from integrations.ulip import (
    REF_TYPE_CONTAINER,
    REF_TYPE_VEHICLE,
    UlipClient,
    UlipError,
    normalize_container_events,
    normalize_vehicle_events,
)

from .repository import LogisticsRepository

log = get_logger("services.logistics.service")

# Cache-key convention matches gateway/cache.py: jnpa:cache:{api}:{key}.
CACHE_PREFIX = "jnpa:cache:ulip"
DEFAULT_CACHE_TTL_S = 300
# A successful ULIP call within this window makes the summary LIVE.
DEFAULT_FRESH_WINDOW_S = 900

PATH_LIVE = "LIVE"
PATH_CACHED = "CACHED"
PATH_DATABASE = "DATABASE"
PATH_FALLBACK = "FALLBACK"
_PATH_SOURCE = {PATH_LIVE: "ULIP", PATH_CACHED: "ULIP_CACHE",
                PATH_DATABASE: "ULIP_DB", PATH_FALLBACK: "NONE"}

STATUS_LIVE = "LIVE"
STATUS_DEGRADED = "DEGRADED"
STATUS_OFFLINE = "OFFLINE"

# ISO-6346 container number: 3 owner letters + category letter + 7 digits.
_CONTAINER_RE = re.compile(r"^[A-Z]{3}[UJZ]\d{7}$")

# A reference is IN_TRANSIT when its newest event is younger than this.
_IN_TRANSIT_WINDOW_S = 24 * 3600

# Events returned inline with a tracking answer (full history via /events).
_TRACKING_EVENT_LIMIT = 20


def cache_key_tracking(ref_id: str) -> str:
    return f"{CACHE_PREFIX}:tracking:{ref_id.strip().upper()}"


def cache_key_summary() -> str:
    return f"{CACHE_PREFIX}:summary"


def classify_ref(ref_id: str) -> str:
    """VEHICLE or CONTAINER for one reference id (ISO-6346 -> CONTAINER)."""
    return (REF_TYPE_CONTAINER if _CONTAINER_RE.match(ref_id.strip().upper())
            else REF_TYPE_VEHICLE)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def empty_tracking(ref_id: str, ref_type: str) -> Dict[str, Any]:
    """The FALLBACK rung: explicitly empty, clearly tagged — never fabricated."""
    return {
        "ref_id": ref_id.strip().upper(),
        "ref_type": ref_type,
        "tracking_status": "UNKNOWN",
        "last_event": None,
        "last_location": None,
        "last_event_ts": None,
        "event_count": 0,
        "events": [],
        "data_available": False,
    }


def empty_summary() -> Dict[str, Any]:
    """The FALLBACK rung for the summary surface: zeros, clearly tagged."""
    return {
        "window_h": 24,
        "event_count": 0,
        "vehicle_count": 0,
        "container_count": 0,
        "events_by_type": {},
        "last_event_ts": None,
        "latest_events": [],
        "tracked": [],
        "data_available": False,
    }


# Module-level cache primitives (monkeypatchable in tests; best-effort like
# services.traffic.service — a Redis outage must never fail the request).
async def _cache_put(key: str, value: Dict[str, Any], ttl: int) -> None:
    wrapped = {"cached_at": _now_iso(), "value": value}
    try:
        await redis_io.cache_set(key, wrapped, ttl=ttl)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("logistics_cache_put_failed", key=key, error=str(exc))


async def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    try:
        raw = await redis_io.cache_get(key)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("logistics_cache_get_failed", key=key, error=str(exc))
        return None
    if not isinstance(raw, dict) or "value" not in raw:
        return None
    return {"value": raw["value"], "cached_at": raw.get("cached_at"),
            "age_s": _age_seconds(raw.get("cached_at"))}


def _age_seconds(cached_at: Optional[str]) -> Optional[float]:
    if not cached_at:
        return None
    try:
        then = datetime.fromisoformat(cached_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return round((datetime.now(tz=timezone.utc) - then).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


def _tracking_status(last_event_ts: Optional[str], event_count: int) -> str:
    """IN_TRANSIT (recent movement) / IDLE (history but stale) / UNKNOWN."""
    if event_count <= 0:
        return "UNKNOWN"
    age = _age_seconds(last_event_ts)
    if age is not None and age <= _IN_TRANSIT_WINDOW_S:
        return "IN_TRANSIT"
    return "IDLE"


class LogisticsService:
    """Fetch and normalise ULIP logistics intelligence with the
    LIVE -> CACHED -> DATABASE -> FALLBACK chain."""

    def __init__(
        self,
        dsn: Optional[str] = None,
        *,
        client: Optional[UlipClient] = None,
        repository: Optional[LogisticsRepository] = None,
        cache_ttl_s: int = DEFAULT_CACHE_TTL_S,
        fresh_window_s: int = DEFAULT_FRESH_WINDOW_S,
    ) -> None:
        self._client = client or UlipClient()
        self._repo = repository or LogisticsRepository(dsn)
        self.cache_ttl_s = cache_ttl_s
        self.fresh_window_s = fresh_window_s

    @property
    def configured(self) -> bool:
        """True when the ULIP provider participates (a credential is present)."""
        return self._client.configured

    # ---------------------------------------------------------------- tracking
    async def tracking(self, ref_id: str) -> Dict[str, Any]:
        """Tracking for one vehicle registration / container number. Never
        raises for an upstream failure — it degrades through CACHED and
        DATABASE to the empty FALLBACK and says so in the metadata."""
        ref = ref_id.strip().upper()
        ref_type = classify_ref(ref)
        key = cache_key_tracking(ref)

        block: Optional[Dict[str, Any]] = None
        path = PATH_LIVE
        cache_age_s: Optional[float] = None

        # -------------------------------------------------- LIVE rung (ULIP)
        if self.configured:
            block = await self._fetch_live(ref, ref_type)
        else:
            log.debug("logistics_provider_disabled")

        # ------------------------------------------------ CACHED rung (Redis)
        if block is None:
            cached = await _cache_get(key)
            if cached is not None and cached["value"]:
                block, path = cached["value"], PATH_CACHED
                cache_age_s = cached.get("age_s")

        # -------------------------------------------- DATABASE rung (Postgres)
        if block is None:
            block = await self._db_fallback(ref)
            if block is not None:
                path = PATH_DATABASE

        # ---------------------------------------------- FALLBACK rung (empty)
        if block is None:
            block, path = empty_tracking(ref, ref_type), PATH_FALLBACK

        # ------------------------------------ write-back + persist (LIVE only)
        if path == PATH_LIVE:
            await _cache_put(key, block, self.cache_ttl_s)
            await self._persist_tracking(block)

        if path == PATH_LIVE:
            status = STATUS_LIVE
        elif path == PATH_FALLBACK:
            status = STATUS_OFFLINE
        else:
            status = STATUS_DEGRADED

        return {
            "status": status,
            "source": _PATH_SOURCE[path],
            "decision_path": path,
            "tracking": block,
            "cache_age_s": cache_age_s,
            "timestamp": _now_iso(),
        }

    # ----------------------------------------------------------------- summary
    async def current(self) -> Dict[str, Any]:
        """Corridor-level logistics summary (event volumes, tracked
        references, latest events). The data itself is the persisted event
        store (ULIP is queried per-reference, not corridor-wide), so the
        chain here is CACHED -> DATABASE -> FALLBACK; ``status`` reflects how
        fresh the newest successful ULIP call is."""
        key = cache_key_summary()
        block: Optional[Dict[str, Any]] = None
        path = PATH_DATABASE
        cache_age_s: Optional[float] = None

        cached = await _cache_get(key)
        if cached is not None and cached["value"]:
            block, path = cached["value"], PATH_CACHED
            cache_age_s = cached.get("age_s")

        if block is None:
            try:
                summary = await self._repo.summary(window_h=24)
                latest = await self._repo.list_events(limit=10)
                tracked = await self._repo.list_tracking(limit=10)
                block = {
                    "window_h": 24,
                    "event_count": int(summary.get("event_count") or 0),
                    "vehicle_count": int(summary.get("vehicle_count") or 0),
                    "container_count": int(summary.get("container_count") or 0),
                    "events_by_type": summary.get("events_by_type") or {},
                    "last_event_ts": _iso_or_none(summary.get("last_event_ts")),
                    "latest_events": [_public_event(e) for e in latest],
                    "tracked": [_public_tracking(t) for t in tracked],
                    "data_available": bool(int(summary.get("event_count") or 0)
                                           or tracked),
                }
                await _cache_put(key, block, min(self.cache_ttl_s, 60))
            except Exception as exc:  # noqa: BLE001 - DB down => keep degrading
                log.warning("logistics_summary_db_failed", error=str(exc))

        if block is None:
            block, path = empty_summary(), PATH_FALLBACK

        freshness = await self._freshness()
        if path == PATH_FALLBACK:
            status = STATUS_OFFLINE
        elif block.get("data_available") and freshness.get("fresh"):
            status = STATUS_LIVE
        elif block.get("data_available"):
            status = STATUS_DEGRADED
        else:
            status = STATUS_OFFLINE

        return {
            "status": status,
            "source": _PATH_SOURCE[path],
            "decision_path": path,
            "logistics": block,
            "ulip": {"configured": self.configured, **freshness},
            "cache_age_s": cache_age_s,
            "timestamp": _now_iso(),
        }

    # ------------------------------------------------------------------ events
    async def events(self, *, ref_id: Optional[str] = None,
                     event_type: Optional[str] = None,
                     limit: int = 100, offset: int = 0) -> Tuple[list, int]:
        """Persisted event history (newest first) + total count."""
        items = await self._repo.list_events(ref_id=ref_id, event_type=event_type,
                                             limit=limit, offset=offset)
        total = await self._repo.count_events(ref_id=ref_id, event_type=event_type)
        return [_public_event(e) for e in items], total

    # ------------------------------------------------------------------ health
    async def health(self) -> Dict[str, Any]:
        """ULIP integration posture (no credential material is ever returned)."""
        client = self._client
        posture: Dict[str, Any] = {
            "system": "LOGISTICS",
            "provider": "ULIP",
            "configured": client.configured,
            "auth_mode": client.auth_mode,
            "api_url": client.api_url,
            "apis": {"vehicle": client.fastag_api, "container": client.ldb_api},
            "timeout_s": client.timeout_s,
            "retries": client.retries,
            "cache_ttl_s": self.cache_ttl_s,
        }
        posture.update(await self._freshness())
        return posture

    # ----------------------------------------------------------------- helpers
    async def _fetch_live(self, ref: str, ref_type: str) -> Optional[Dict[str, Any]]:
        """One audited ULIP fetch, normalised into a tracking block; None on
        any client failure (the caller keeps degrading)."""
        api_name = (self._client.ldb_api if ref_type == REF_TYPE_CONTAINER
                    else self._client.fastag_api)
        t0 = time.perf_counter()
        try:
            if ref_type == REF_TYPE_CONTAINER:
                envelope = await self._client.fetch_container_tracking(ref)
                events = normalize_container_events(envelope, ref)
            else:
                envelope = await self._client.fetch_vehicle_movement(ref)
                events = normalize_vehicle_events(envelope, ref)
        except UlipError as exc:
            await self._audit(api_name=api_name, ref_type=ref_type, ref_id=ref,
                              ok=False, latency_ms=_ms_since(t0), error=str(exc))
            log.warning("logistics_live_failed", ref=ref, error=str(exc))
            return None
        await self._audit(api_name=api_name, ref_type=ref_type, ref_id=ref,
                          ok=True, http_status=200, latency_ms=_ms_since(t0),
                          response={"code": str(envelope.code),
                                    "message": envelope.message,
                                    "events": len(events)})
        await self._persist_events(events)
        newest = events[0] if events else None
        return {
            "ref_id": ref,
            "ref_type": ref_type,
            "tracking_status": _tracking_status(
                newest["event_ts"] if newest else None, len(events)),
            "last_event": newest["event_type"] if newest else None,
            "last_location": newest["location"] if newest else None,
            "last_event_ts": newest["event_ts"] if newest else None,
            "event_count": len(events),
            "events": [_public_event(e) for e in events[:_TRACKING_EVENT_LIMIT]],
            "data_available": bool(events),
        }

    async def _db_fallback(self, ref: str) -> Optional[Dict[str, Any]]:
        """Rebuild a tracking block from the persisted snapshot + events."""
        try:
            snapshot = await self._repo.get_tracking(ref)
            events = await self._repo.list_events(ref_id=ref,
                                                  limit=_TRACKING_EVENT_LIMIT)
        except Exception as exc:  # noqa: BLE001 - DB down => keep degrading
            log.warning("logistics_db_fallback_failed", ref=ref, error=str(exc))
            return None
        if snapshot is None and not events:
            return None
        newest = events[0] if events else None
        return {
            "ref_id": ref,
            "ref_type": (snapshot or {}).get("ref_type") or classify_ref(ref),
            "tracking_status": (snapshot or {}).get("status")
                               or _tracking_status(
                                   newest.get("event_ts") if newest else None,
                                   len(events)),
            "last_event": (snapshot or {}).get("last_event")
                          or (newest.get("event_type") if newest else None),
            "last_location": (snapshot or {}).get("last_location")
                             or (newest.get("location") if newest else None),
            "last_event_ts": (snapshot or {}).get("last_event_ts")
                             or (newest.get("event_ts") if newest else None),
            "event_count": int((snapshot or {}).get("event_count") or len(events)),
            "events": [_public_event(e) for e in events],
            "data_available": True,
        }

    async def _persist_events(self, events: List[Dict[str, Any]]) -> None:
        """Append normalised events (best-effort, deduped by the repository)."""
        if not events:
            return
        try:
            inserted = await self._repo.insert_events(events)
            log.info("logistics_events_persisted", new=inserted,
                     fetched=len(events))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("logistics_events_persist_failed", error=str(exc))

    async def _persist_tracking(self, block: Dict[str, Any]) -> None:
        """Refresh the snapshot row for the tracked reference (best-effort)."""
        try:
            await self._repo.upsert_tracking(
                ref_type=block["ref_type"], ref_id=block["ref_id"],
                status=block["tracking_status"],
                last_event=block.get("last_event"),
                last_location=block.get("last_location"),
                last_event_ts=block.get("last_event_ts"),
                event_count=int(block.get("event_count") or 0),
                source="ULIP",
                payload={"tracking": {k: v for k, v in block.items()
                                      if k != "events"}},
            )
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            log.warning("logistics_tracking_persist_failed",
                        ref=block.get("ref_id"), error=str(exc))

    async def _audit(self, **kwargs: Any) -> None:
        """Append one ULIP call audit row (best-effort)."""
        try:
            await self._repo.insert_audit(**kwargs)
        except Exception as exc:  # noqa: BLE001 - audit is best-effort
            log.warning("logistics_audit_failed", error=str(exc))

    async def _freshness(self) -> Dict[str, Any]:
        """Metadata about the newest ULIP call (drives LIVE vs DEGRADED)."""
        try:
            audit = await self._repo.last_audit()
        except Exception as exc:  # noqa: BLE001 - DB down => unknown freshness
            log.debug("logistics_freshness_failed", error=str(exc))
            audit = None
        if not audit:
            return {"last_call_at": None, "last_call_ok": None, "fresh": False}
        created = audit.get("created_at")
        created_iso = created.isoformat() if isinstance(created, datetime) else created
        age = _age_seconds(created_iso)
        return {
            "last_call_at": created_iso,
            "last_call_ok": bool(audit.get("ok")),
            "fresh": bool(audit.get("ok")) and age is not None
                     and age <= self.fresh_window_s,
        }


def _ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _iso_or_none(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    return value if isinstance(value, str) else None


def _public_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """The client-facing event shape (raw upstream detail stays server-side
    in the DB/audit tables — the API returns only the normalised fields)."""
    return {
        "ref_type": event.get("ref_type"),
        "ref_id": event.get("ref_id"),
        "event_type": event.get("event_type"),
        "event_ts": _iso_or_none(event.get("event_ts")),
        "location": event.get("location"),
        "latitude": _float_or_none(event.get("latitude")),
        "longitude": _float_or_none(event.get("longitude")),
        "source": event.get("source") or "ULIP",
        "source_api": event.get("source_api"),
    }


def _public_tracking(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ref_type": row.get("ref_type"),
        "ref_id": row.get("ref_id"),
        "status": row.get("status"),
        "last_event": row.get("last_event"),
        "last_location": row.get("last_location"),
        "last_event_ts": _iso_or_none(row.get("last_event_ts")),
        "event_count": int(row.get("event_count") or 0),
        "updated_at": _iso_or_none(row.get("updated_at")),
    }


def _float_or_none(value: Any) -> Optional[float]:
    """DB numerics surface as Decimal — JSON-safe floats out, None-safe."""
    return float(value) if value is not None else None


__all__ = ["LogisticsService", "cache_key_tracking", "cache_key_summary",
           "classify_ref", "empty_tracking", "empty_summary",
           "STATUS_LIVE", "STATUS_DEGRADED", "STATUS_OFFLINE",
           "PATH_LIVE", "PATH_CACHED", "PATH_DATABASE", "PATH_FALLBACK"]
