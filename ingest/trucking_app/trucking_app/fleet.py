"""The fleet: owns every truck, builds routes, and reads congestion from Redis.

Responsibilities
  * Deterministically build N truck profiles at start (plate linked to the Vahan
    simulator, device_id ``TRK-000001`` …, gate round-robin, origin within
    100 km). Same seed -> same fleet, host-to-host.
  * Bind routes to trucks that need one (driving state but no polyline), using
    the async ``Router`` (OSRM -> HERE -> dead reckoning).
  * Provide the current jam factor for a corridor segment, read from the Redis
    key ``traffic:segment:{id}:jam_factor`` (written by the dashboard) with a
    short in-process cache so 20k trucks don't each hit Redis every tick.
  * Hot-scale the population up/down and apply per-device route overrides
    (used by the TFC-1 gate-closure scenario).

The fleet is pure state + async helpers; the per-truck publish/advance loop lives
in ``simulator.py`` and the control plane in ``app.py``.
"""
from __future__ import annotations

import asyncio
import random
import time
from typing import Dict, List, Optional, Tuple

import redis.asyncio as aioredis

from jnpa_shared.logging import get_logger

from . import gates, plates
from .config import REDIS_JAM_KEY_FMT, TruckConfig
from .metrics import TRUCKS_BY_STATE, TRUCKS_TOTAL
from .routing import Router
from .truck import Truck, TruckProfile, TruckState

log = get_logger("trucking_app.fleet")

LatLon = Tuple[float, float]


def build_profile(i: int, cfg: TruckConfig, rng: random.Random) -> TruckProfile:
    """Deterministic profile for device index ``i`` (0-based)."""
    gate_id = cfg.gate_ids[i % len(cfg.gate_ids)]
    return TruckProfile(
        device_id=f"TRK-{i + 1:06d}",
        plate=plates.plate_for_index(i),
        gate_id=gate_id,
        origin=gates.random_origin(rng, gate_id, cfg.origin_radius_km),
    )


class Fleet:
    """The full population of trucks plus routing + congestion services."""

    def __init__(self, cfg: TruckConfig) -> None:
        self.cfg = cfg
        self.router = Router(cfg)
        self.trucks: Dict[str, Truck] = {}
        self._order: List[str] = []  # stable device order for round-robin scans
        self._redis: Optional[aioredis.Redis] = None
        # Per-segment jam cache: segment_id -> (jam_factor, fetched_monotonic).
        self._jam_cache: Dict[str, Tuple[float, float]] = {}
        self._jam_ttl_s = 5.0
        self._next_index = 0  # next device index to mint when scaling up

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> None:
        await self.router.start()
        try:
            self._redis = aioredis.from_url(
                self.cfg.redis_url, encoding="utf-8", decode_responses=True
            )
            await self._redis.ping()
            log.info("redis_connected", url=self.cfg.redis_url)
        except Exception as exc:  # noqa: BLE001 - jam factor is best-effort
            log.warning("redis_unavailable", error=str(exc))
            self._redis = None
        self.populate(self.cfg.num_devices)

    async def close(self) -> None:
        await self.router.close()
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    # -- population ---------------------------------------------------------
    def populate(self, n: int) -> None:
        """Build exactly ``n`` trucks from a deterministic seed (replaces fleet)."""
        rng = random.Random(self.cfg.seed)
        self.trucks.clear()
        self._order.clear()
        for i in range(n):
            self._add_truck(i, rng)
        self._next_index = n
        self._refresh_population_metrics()
        log.info("fleet_populated", devices=n, seed=self.cfg.seed)

    def _add_truck(self, i: int, rng: random.Random) -> Truck:
        profile = build_profile(i, self.cfg, rng)
        # Per-truck RNG seeded by index so each truck's noise/dwell is stable and
        # independent of fleet build order.
        truck = Truck(
            profile=profile,
            cfg=self.cfg,
            rng=random.Random(self.cfg.seed * 1_000_003 + i),
        )
        # Stagger initial state so the fleet isn't all departing at t=0.
        truck.state = _staggered_initial_state(i)
        if truck.state == TruckState.AT_GATE_QUEUE:
            truck.position = gates.GATE_COORDS[profile.gate_id]
            truck.dwell_left_s = truck._jittered(self.cfg.gate_queue_dwell_s)
        elif truck.state == TruckState.INSIDE_PORT:
            truck.position = gates.GATE_COORDS[profile.gate_id]
            truck.dwell_left_s = truck._jittered(self.cfg.inside_port_dwell_s)
        elif truck.state == TruckState.IDLE:
            truck.position = profile.origin
            truck.dwell_left_s = truck._jittered(self.cfg.idle_dwell_s)
        self.trucks[profile.device_id] = truck
        self._order.append(profile.device_id)
        return truck

    async def scale_to(self, target: int) -> int:
        """Hot-scale the population to ``target`` (bounded by max_devices)."""
        target = max(0, min(target, self.cfg.max_devices))
        cur = len(self.trucks)
        if target > cur:
            rng = random.Random(self.cfg.seed)
            for i in range(self._next_index, self._next_index + (target - cur)):
                self._add_truck(i, rng)
            self._next_index += target - cur
        elif target < cur:
            for device_id in self._order[target:]:
                self.trucks.pop(device_id, None)
            self._order = self._order[:target]
        self._refresh_population_metrics()
        log.info("fleet_scaled", from_=cur, to=len(self.trucks), target=target)
        return len(self.trucks)

    # -- scenario injection -------------------------------------------------
    def inject_synthetic(
        self,
        *,
        count: int,
        tag: str,
        gate_id: Optional[str] = None,
        state: str = "EN_ROUTE_TO_PORT",
    ) -> List[str]:
        """Create ``count`` scenario-tagged trucks (what-if scenarios, Prompt 10).

        Tagged with ``tag`` so ``remove_tagged(tag)`` can delete exactly these on
        "Reset to baseline". Returns the new device ids. Bounded by max_devices.
        Idempotent per call only by construction (each call mints fresh indices);
        scenarios pass a stable tag so reset is total regardless of call count.
        """
        try:
            want_state = TruckState(state)
        except ValueError:
            want_state = TruckState.EN_ROUTE_TO_PORT
        rng = random.Random(self.cfg.seed)
        room = max(0, self.cfg.max_devices - len(self.trucks))
        n = min(count, room)
        new_ids: List[str] = []
        for k in range(n):
            i = self._next_index + k
            gid = gate_id or self.cfg.gate_ids[i % len(self.cfg.gate_ids)]
            profile = TruckProfile(
                device_id=f"SYN-{tag}-{i + 1:06d}",
                plate=plates.plate_for_index(i),
                gate_id=gid,
                origin=gates.random_origin(rng, gid, self.cfg.origin_radius_km),
                scenario_tag=tag,
            )
            truck = Truck(
                profile=profile, cfg=self.cfg,
                rng=random.Random(self.cfg.seed * 1_000_003 + i),
            )
            truck.state = want_state
            if want_state == TruckState.AT_GATE_QUEUE:
                truck.position = gates.GATE_COORDS[gid]
                truck.dwell_left_s = truck._jittered(self.cfg.gate_queue_dwell_s)
            else:
                truck.position = profile.origin
            self.trucks[profile.device_id] = truck
            self._order.append(profile.device_id)
            new_ids.append(profile.device_id)
        self._next_index += n
        self._refresh_population_metrics()
        log.info("synthetic_injected", tag=tag, count=len(new_ids), gate_id=gate_id, state=state)
        return new_ids

    def remove_tagged(self, tag: str) -> int:
        """Remove every scenario-tagged truck for ``tag``. Returns the count."""
        victims = [
            did for did, t in self.trucks.items()
            if t.profile.scenario_tag == tag
        ]
        for did in victims:
            self.trucks.pop(did, None)
        self._order = [d for d in self._order if d in self.trucks]
        self._refresh_population_metrics()
        log.info("synthetic_removed", tag=tag, count=len(victims))
        return len(victims)

    # -- routing ------------------------------------------------------------
    async def ensure_route(self, truck: Truck) -> None:
        """Bind a route to a truck that needs one (driving with no polyline)."""
        if not truck.needs_route:
            return
        origin = truck.position
        dest = truck.target
        route = await self.router.route(origin, dest)
        truck.set_route(route.points)

    async def override_route(
        self, device_id: str, dest: LatLon, *, force_state: Optional[str] = None
    ) -> bool:
        """Replace a truck's active route with one to ``dest`` (scenario hook).

        Used by Prompt 8's TFC-1 gate-closure scenario to reroute trucks to an
        alternate gate. Returns False if the device is unknown.
        """
        truck = self.trucks.get(device_id)
        if truck is None:
            return False
        if force_state:
            try:
                truck.state = TruckState(force_state)
            except ValueError:
                truck.state = TruckState.EN_ROUTE_TO_PORT
        elif truck.state not in {TruckState.EN_ROUTE_TO_PORT, TruckState.EN_ROUTE_HOME}:
            truck.state = TruckState.EN_ROUTE_TO_PORT
        route = await self.router.route(truck.position, dest)
        truck.set_route(route.points)
        log.info("route_overridden", device_id=device_id, dest=dest, points=len(route.points))
        return True

    async def compute_eta_s(self, truck: Truck) -> Optional[float]:
        """ETA to the target gate (seconds), preferring a live OSRM/HERE duration."""
        if truck.state in {TruckState.INSIDE_PORT, TruckState.IDLE}:
            return 0.0
        dest = gates.GATE_COORDS[truck.profile.gate_id]
        live = await self.router.duration_s(truck.position, dest)
        if live is not None:
            truck.eta_s = live
            return live
        # Fallback: remaining route distance at the highway free-flow speed.
        rem_km = truck.remaining_km or 0.0
        eta = (rem_km / max(1e-6, self.cfg.speed_highway_kmh)) * 3600.0
        truck.eta_s = eta
        return eta

    # -- congestion ---------------------------------------------------------
    async def jam_factor(self, segment_id: Optional[str]) -> float:
        """Current jam factor (0..10) for a segment, cached for ``_jam_ttl_s``."""
        if not segment_id or self._redis is None:
            return 0.0
        now = time.monotonic()
        cached = self._jam_cache.get(segment_id)
        if cached is not None and now - cached[1] < self._jam_ttl_s:
            return cached[0]
        value = 0.0
        try:
            raw = await self._redis.get(REDIS_JAM_KEY_FMT.format(segment_id=segment_id))
            if raw is not None:
                value = max(0.0, min(10.0, float(raw)))
        except Exception as exc:  # noqa: BLE001 - congestion is best-effort
            log.debug("jam_lookup_failed", segment_id=segment_id, error=str(exc))
        self._jam_cache[segment_id] = (value, now)
        return value

    def jam_factor_cached(self, segment_id: Optional[str]) -> float:
        """Synchronous read of the jam cache (0.0 on miss).

        The tick warms this via ``jam_factor`` for every due segment first, so
        the per-truck advance reads it without awaiting Redis on the hot path.
        """
        if not segment_id:
            return 0.0
        cached = self._jam_cache.get(segment_id)
        return cached[0] if cached is not None else 0.0

    # -- stats --------------------------------------------------------------
    def population_stats(self) -> dict:
        counts: Dict[str, int] = {s.value: 0 for s in TruckState}
        for t in self.trucks.values():
            counts[t.state.value] += 1
        return {
            "population": len(self.trucks),
            "target_default": self.cfg.num_devices,
            "max_devices": self.cfg.max_devices,
            "by_state": counts,
        }

    def _refresh_population_metrics(self) -> None:
        TRUCKS_TOTAL.set(len(self.trucks))
        counts: Dict[str, int] = {s.value: 0 for s in TruckState}
        for t in self.trucks.values():
            counts[t.state.value] += 1
        for state, n in counts.items():
            TRUCKS_BY_STATE.labels(state).set(n)


def _staggered_initial_state(i: int) -> TruckState:
    """Spread the starting fleet across states so telemetry looks live at t=0."""
    bucket = i % 10
    if bucket < 5:
        return TruckState.EN_ROUTE_TO_PORT   # 50% inbound
    if bucket < 7:
        return TruckState.AT_GATE_QUEUE       # 20% queueing
    if bucket < 8:
        return TruckState.INSIDE_PORT         # 10% inside
    if bucket < 9:
        return TruckState.EN_ROUTE_HOME       # 10% homeward
    return TruckState.IDLE                     # 10% idle
