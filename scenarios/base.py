"""Shared helpers the three scenarios build on.

* HTTP helpers against the gateway / truck-sim / congestion / anomaly.
* ``nudge_segments`` — writes high-jam ``jnpa.traffic_snapshots`` rows for a set
  of corridor segments so the forecaster's feature window (and the live map
  overlay) actually reflect a build-up. Per the design decision, scenarios are
  "best-effort + nudge": they make the world reflect the scenario, poll the
  forecaster, and record whether the P>=threshold assertion was met without
  hard-failing the run.
* ``poll_forecaster`` — polls ``/predict`` until enough segments cross a
  probability threshold or attempts run out; returns (met, probs, crossed).
* ``clear_nudge`` — deletes the scenario's synthetic snapshots on reset.

All DB writes tag rows with ``source='scenario:<handle>'`` so reset removes
exactly what the scenario injected.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from jnpa_shared.logging import get_logger

from .config import ScenarioConfig

log = get_logger("scenarios.base")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class Upstreams:
    """Thin async client bundle for the services a scenario orchestrates."""

    def __init__(self, cfg: ScenarioConfig) -> None:
        self.cfg = cfg
        self.http = httpx.AsyncClient(timeout=cfg.upstream_timeout_s)

    async def aclose(self) -> None:
        await self.http.aclose()

    # -- gateway --
    async def gw_post(self, path: str, json: Any) -> Optional[dict]:
        return await self._req("POST", self.cfg.gateway_url, path, json)

    async def gw_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        return await self._req("GET", self.cfg.gateway_url, path, None, params)

    # -- truck sim --
    async def truck_post(self, path: str, json: Any) -> Optional[dict]:
        return await self._req("POST", self.cfg.truck_api_url, path, json)

    async def truck_delete(self, path: str) -> Optional[dict]:
        return await self._req("DELETE", self.cfg.truck_api_url, path, None)

    async def truck_get(self, path: str, params: Optional[dict] = None) -> Optional[dict]:
        return await self._req("GET", self.cfg.truck_api_url, path, None, params)

    # -- congestion --
    async def predict(self, horizon_min: int = 15) -> Dict[str, float]:
        data = await self._req("POST", self.cfg.congestion_url, "/predict",
                               {"horizon_min": horizon_min})
        return data if isinstance(data, dict) else {}

    async def _req(self, method: str, base: str, path: str,
                   json: Any = None, params: Optional[dict] = None) -> Optional[dict]:
        url = base.rstrip("/") + path
        try:
            resp = await self.http.request(method, url, json=json, params=params)
            if resp.status_code < 400:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            log.debug("upstream_non2xx", url=url, status=resp.status_code)
            return None
        except httpx.HTTPError as exc:
            log.debug("upstream_unreachable", url=url, error=str(exc))
            return None


# --------------------------------------------------------------------------- congestion nudge
async def nudge_segments(
    cfg: ScenarioConfig, segment_ids: List[str], *, handle_id: str,
    jam_factor: float = 7.5, speed_kmh: float = 8.0,
) -> int:
    """Write high-jam traffic_snapshots for ``segment_ids`` (tagged for reset).

    jam_factor is on the 0..10 schema scale; 7.5 ~= 0.75 normalised (well above
    the 0.6/0.7 assertion bands). Returns rows written.
    """
    from jnpa_shared.db import execute
    src = f"scenario:{handle_id}"
    n = 0
    ts = _now()
    for seg in segment_ids:
        try:
            await execute(
                """
                INSERT INTO jnpa.traffic_snapshots (ts, segment_id, speed_kmh, jam_factor, source)
                VALUES (:ts, :seg, :spd, :jam, :src)
                """,
                {"ts": ts, "seg": seg, "spd": speed_kmh, "jam": jam_factor, "src": src},
                dsn=cfg.postgres_dsn,
            )
            n += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("nudge_write_failed", segment=seg, error=str(exc))
    log.info("segments_nudged", handle=handle_id, count=n, jam=jam_factor)
    return n


async def clear_nudge(cfg: ScenarioConfig, handle_id: str) -> int:
    """Delete the synthetic snapshots this scenario wrote (reset)."""
    from jnpa_shared.db import execute
    try:
        return await execute(
            "DELETE FROM jnpa.traffic_snapshots WHERE source = :src",
            {"src": f"scenario:{handle_id}"}, dsn=cfg.postgres_dsn,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("clear_nudge_failed", handle=handle_id, error=str(exc))
        return 0


async def poll_forecaster(
    up: Upstreams, *, segment_ids: List[str], threshold: float, need: int,
    horizon_min: int = 15,
) -> Tuple[bool, Dict[str, float], List[str]]:
    """Poll /predict until ``need`` of ``segment_ids`` cross ``threshold``.

    Returns (met, latest_probs, crossed_segments). Best-effort: stops after
    cfg.forecast_poll_attempts regardless, returning whatever it last saw.
    """
    cfg = up.cfg
    last: Dict[str, float] = {}
    crossed: List[str] = []
    for _ in range(max(1, cfg.forecast_poll_attempts)):
        probs = await up.predict(horizon_min)
        if probs:
            last = probs
            crossed = [s for s in segment_ids if float(probs.get(s, 0.0)) >= threshold]
            if len(crossed) >= need:
                return True, last, crossed
        await asyncio.sleep(cfg.forecast_poll_interval_s)
    return (len(crossed) >= need), last, crossed


__all__ = ["Upstreams", "nudge_segments", "clear_nudge", "poll_forecaster"]
