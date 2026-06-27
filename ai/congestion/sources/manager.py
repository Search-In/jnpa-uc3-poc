"""SourceManager — cascading traffic-speed resolver with Redis caching.

``get(seg)`` resolves the current speed for a corridor segment by:

  1. Returning a fresh Redis-cached reading if one exists (< 90 s old).
  2. Otherwise trying each configured source IN ORDER (google -> here ->
     tomtom), giving each a 1-second timeout. The first success is cached in
     Redis for 90 s and returned.
  3. If ALL sources fail (timeout/error/None), returning the LAST cached value
     marked ``stale=true`` — the graceful-degradation foundation for
     Sub-Criterion 3. If nothing is cached either, a synthetic reading is
     returned (also marked stale) so the prediction loop never starves.

The cache key is ``traffic:speed:{segment_id}``; a separate
``traffic:speed:last:{segment_id}`` key holds the last-known value with NO TTL
so the stale fallback survives the 90-s window.
"""
from __future__ import annotations

import asyncio
import json
from typing import Dict, List, Optional

from jnpa_shared.logging import get_logger
from jnpa_shared import redis_io

from ..config import CongestionConfig
from ..graph import SegmentMeta
from .base import SpeedReading, synthetic_speed
from .google import GoogleSource
from .here import HereSource
from .tomtom import TomTomSource

log = get_logger("congestion.sources")

_FACTORY = {"google": GoogleSource, "here": HereSource, "tomtom": TomTomSource}


class SourceManager:
    def __init__(self, cfg: CongestionConfig) -> None:
        self.cfg = cfg
        keys = {
            "google": cfg.google_maps_api_key,
            "here": cfg.here_api_key,
            "tomtom": cfg.tomtom_api_key,
        }
        self.sources = [
            _FACTORY[name](api_key=keys.get(name, ""), free_flow_kmh=cfg.free_flow_speed_kmh)
            for name in cfg.source_order
            if name in _FACTORY
        ]

    def _cache_key(self, seg_id: str) -> str:
        return f"traffic:speed:{seg_id}"

    def _last_key(self, seg_id: str) -> str:
        return f"traffic:speed:last:{seg_id}"

    async def get(self, seg: SegmentMeta) -> SpeedReading:
        """Resolve the current speed for ``seg`` (cache -> cascade -> stale)."""
        # 1. fresh cache
        cached = await self._cache_get(self._cache_key(seg.id))
        if cached is not None:
            return cached

        # 2. cascade with a per-source 1-second timeout
        for src in self.sources:
            try:
                reading = await asyncio.wait_for(
                    src.get_segment_speed(seg), timeout=self.cfg.source_timeout_s
                )
            except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
                log.debug("source_failed", source=src.name, seg=seg.id, error=str(exc))
                reading = None
            if reading is not None:
                await self._cache_put(seg.id, reading)
                return reading

        # 3. stale fallback (last-known, then synthetic)
        last = await self._cache_get(self._last_key(seg.id))
        if last is not None:
            last.stale = True
            log.info("source_stale_fallback", seg=seg.id, source=last.source)
            return last
        fallback = synthetic_speed(seg, self.cfg.free_flow_speed_kmh)
        fallback.stale = True
        fallback.source = "stale-synthetic"
        return fallback

    async def get_all(self, segs: List[SegmentMeta]) -> Dict[str, SpeedReading]:
        readings = await asyncio.gather(*(self.get(s) for s in segs))
        return {r.segment_id: r for r in readings}

    # ----------------------------------------------------------------- cache io
    async def _cache_get(self, key: str) -> Optional[SpeedReading]:
        try:
            raw = await redis_io.cache_get(key)
        except Exception:  # noqa: BLE001 - redis down: behave as cache miss
            return None
        if not raw:
            return None
        try:
            return SpeedReading(**raw)
        except (TypeError, ValueError):
            return None

    async def _cache_put(self, seg_id: str, reading: SpeedReading) -> None:
        payload = reading.as_dict()
        try:
            await redis_io.cache_set(self._cache_key(seg_id), payload, ttl=self.cfg.source_cache_ttl_s)
            # last-known value with no TTL for the stale fallback path
            client = redis_io.get_client()
            await client.set(self._last_key(seg_id), json.dumps(payload, separators=(",", ":")))
        except Exception as exc:  # noqa: BLE001
            log.debug("cache_put_failed", seg=seg_id, error=str(exc))


__all__ = ["SourceManager"]
