"""The per-truck domain model: profile, state machine, and motion/noise model.

A ``Truck`` owns a deterministic profile (plate, device id, target gate) plus
mutable runtime state (position along the active route, state-machine state,
speed). ``advance(dt, jam_factor)`` moves it forward by ``dt`` seconds and is
the single place speed and the state machine evolve; ``telemetry()`` snapshots
the current position with GPS noise applied into a ``TruckTelemetry`` event.

State machine (spec):
    EN_ROUTE_TO_PORT -> AT_GATE_QUEUE -> INSIDE_PORT -> EN_ROUTE_HOME -> IDLE
and back to EN_ROUTE_TO_PORT for the next trip. Routes for each leg are supplied
by the fleet (it owns the async Router); the truck signals when it needs one via
``needs_route``.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Tuple

from jnpa_shared.corridor import haversine_km, nearest_segment
from jnpa_shared.schemas import TruckTelemetry

from . import gates
from .config import TruckConfig

LatLon = Tuple[float, float]


class TruckState(str, Enum):
    EN_ROUTE_TO_PORT = "EN_ROUTE_TO_PORT"
    AT_GATE_QUEUE = "AT_GATE_QUEUE"
    INSIDE_PORT = "INSIDE_PORT"
    EN_ROUTE_HOME = "EN_ROUTE_HOME"
    IDLE = "IDLE"


# States in which the truck is actively driving a route polyline.
_DRIVING = {TruckState.EN_ROUTE_TO_PORT, TruckState.EN_ROUTE_HOME}


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class TruckProfile:
    """Deterministic, stable-per-device identity."""

    device_id: str
    plate: str
    gate_id: str        # target JNPA gate (round-robin assigned)
    origin: LatLon      # home / yard, within 100 km of the gate
    # Set when a what-if scenario injected this truck (e.g. "TFC-1:<handle>").
    # Lets "Reset to baseline" remove exactly the trucks a scenario created.
    scenario_tag: Optional[str] = None


@dataclass
class Truck:
    """A simulated truck device. Mutable runtime state on top of its profile."""

    profile: TruckProfile
    cfg: TruckConfig
    rng: random.Random

    state: TruckState = TruckState.EN_ROUTE_TO_PORT
    # Active route polyline + how far along it we are (km from the route start).
    route_points: List[LatLon] = field(default_factory=list)
    route_cum_km: List[float] = field(default_factory=list)  # cumulative km per point
    dist_along_km: float = 0.0
    position: LatLon = (0.0, 0.0)
    heading: float = 0.0
    speed_kmh: float = 0.0
    battery: float = 100.0
    # Seconds remaining in a dwell state (AT_GATE_QUEUE / INSIDE_PORT / IDLE).
    dwell_left_s: float = 0.0
    # Last computed ETA-to-gate (seconds); refreshed by the fleet every 30 s.
    eta_s: Optional[float] = None

    def __post_init__(self) -> None:
        if self.position == (0.0, 0.0):
            self.position = self.profile.origin

    # -- route binding ------------------------------------------------------
    @property
    def needs_route(self) -> bool:
        """True when in a driving state but without an active route polyline."""
        return self.state in _DRIVING and not self.route_points

    @property
    def target(self) -> LatLon:
        """Where this leg is heading (gate when outbound, origin when homeward)."""
        if self.state == TruckState.EN_ROUTE_HOME:
            return self.profile.origin
        return gates.GATE_COORDS[self.profile.gate_id]

    def set_route(self, points: List[LatLon]) -> None:
        """Bind a new route polyline and reset progress to its start."""
        if not points:
            return
        self.route_points = list(points)
        self.route_cum_km = _cumulative_km(self.route_points)
        self.dist_along_km = 0.0
        self.position = self.route_points[0]
        self.battery = max(5.0, self.battery - self.rng.uniform(0.0, 1.0))
        self._update_heading()

    @property
    def route_length_km(self) -> float:
        return self.route_cum_km[-1] if self.route_cum_km else 0.0

    @property
    def remaining_km(self) -> float:
        return max(0.0, self.route_length_km - self.dist_along_km)

    # -- per-tick advance ---------------------------------------------------
    def advance(self, dt: float, jam_factor: float = 0.0) -> Optional[TruckState]:
        """Advance the truck by ``dt`` seconds. Returns the new state on a
        transition, else ``None``. ``jam_factor`` (0..10) comes from Redis."""
        if self.state in _DRIVING:
            return self._advance_driving(dt, jam_factor)
        return self._advance_dwell(dt)

    def _advance_driving(self, dt: float, jam_factor: float) -> Optional[TruckState]:
        if not self.route_points:
            return None  # waiting for the fleet to bind a route
        self.speed_kmh = self._target_speed(jam_factor)
        step_km = self.speed_kmh * (dt / 3600.0)
        self.dist_along_km = min(self.route_length_km, self.dist_along_km + step_km)
        self.position = self._point_at(self.dist_along_km)
        self._update_heading()

        if self.remaining_km <= 0.02:  # arrived (~20 m)
            return self._on_arrival()
        return None

    def _advance_dwell(self, dt: float) -> Optional[TruckState]:
        self.speed_kmh = 0.0
        self.dwell_left_s -= dt
        if self.dwell_left_s > 0:
            return None
        return self._on_dwell_done()

    # -- state machine ------------------------------------------------------
    def _on_arrival(self) -> TruckState:
        """Finished a driving leg: clear the route and enter the next state."""
        self.route_points = []
        self.route_cum_km = []
        self.dist_along_km = 0.0
        if self.state == TruckState.EN_ROUTE_TO_PORT:
            self.position = gates.GATE_COORDS[self.profile.gate_id]
            self.dwell_left_s = self._jittered(self.cfg.gate_queue_dwell_s)
            return self._enter(TruckState.AT_GATE_QUEUE)
        # EN_ROUTE_HOME -> arrived home -> IDLE
        self.position = self.profile.origin
        self.dwell_left_s = self._jittered(self.cfg.idle_dwell_s)
        return self._enter(TruckState.IDLE)

    def _on_dwell_done(self) -> TruckState:
        if self.state == TruckState.AT_GATE_QUEUE:
            self.dwell_left_s = self._jittered(self.cfg.inside_port_dwell_s)
            return self._enter(TruckState.INSIDE_PORT)
        if self.state == TruckState.INSIDE_PORT:
            # Turn around and head home — fleet will bind the return route.
            return self._enter(TruckState.EN_ROUTE_HOME)
        # IDLE -> start a fresh trip to the port.
        return self._enter(TruckState.EN_ROUTE_TO_PORT)

    def _enter(self, state: TruckState) -> TruckState:
        self.state = state
        return state

    # -- speed / position helpers ------------------------------------------
    def _target_speed(self, jam_factor: float) -> float:
        """Free-flow 55 km/h highway / 25 km/h port roads, Gaussian noise σ=4,
        scaled down by the segment jam factor (queueing pressure)."""
        base = (
            self.cfg.speed_port_kmh
            if gates.is_port_road(*self.position)
            else self.cfg.speed_highway_kmh
        )
        jam_mult = max(0.05, 1.0 - (jam_factor / 10.0) * self.cfg.jam_sensitivity)
        noisy = base * jam_mult + self.rng.gauss(0.0, self.cfg.speed_noise_sigma_kmh)
        return max(0.0, noisy)

    def _point_at(self, dist_km: float) -> LatLon:
        """Interpolate the polyline position at ``dist_km`` from the start."""
        cum = self.route_cum_km
        pts = self.route_points
        if dist_km <= 0:
            return pts[0]
        if dist_km >= cum[-1]:
            return pts[-1]
        # Find the segment containing dist_km (linear scan from a bisect hint).
        lo, hi = 0, len(cum) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] < dist_km:
                lo = mid + 1
            else:
                hi = mid
        i = max(1, lo)
        seg_len = cum[i] - cum[i - 1]
        frac = 0.0 if seg_len <= 0 else (dist_km - cum[i - 1]) / seg_len
        a, b = pts[i - 1], pts[i]
        return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac)

    def _update_heading(self) -> None:
        if self.state in _DRIVING and self.route_points:
            ahead = self._point_at(min(self.route_length_km, self.dist_along_km + 0.1))
            if ahead != self.position:
                self.heading = gates.initial_bearing(self.position, ahead)

    def _jittered(self, base_s: float) -> float:
        """A dwell time with +/-25% jitter so the fleet doesn't move in lockstep."""
        return max(1.0, base_s * self.rng.uniform(0.75, 1.25))

    # -- telemetry snapshot -------------------------------------------------
    def telemetry(self) -> TruckTelemetry:
        """Snapshot the current position with GPS noise as a wire event."""
        lat, lon, acc = self._noisy_position()
        return TruckTelemetry(
            ts=_utcnow(),
            device_id=self.profile.device_id,
            plate=self.profile.plate,
            lat=round(lat, 6),
            lon=round(lon, 6),
            speed_kmh=round(self.speed_kmh, 2),
            heading=round(self.heading, 1),
            battery=round(self.battery, 1),
            accuracy_m=round(acc, 1),
        )

    def _noisy_position(self) -> Tuple[float, float, float]:
        """Apply ε~N(0,6 m) jitter, with a 1% chance of a 50 m outlier."""
        lat, lon = self.position
        if self.rng.random() < self.cfg.gps_outlier_prob:
            sigma_m = self.cfg.gps_outlier_m
            accuracy = self.cfg.gps_outlier_m
        else:
            sigma_m = self.cfg.gps_sigma_m
            accuracy = self.cfg.gps_sigma_m
        # Convert a metre-scale Gaussian offset to degrees at this latitude.
        d_north_m = self.rng.gauss(0.0, sigma_m)
        d_east_m = self.rng.gauss(0.0, sigma_m)
        dlat = d_north_m / 111_320.0
        dlon = d_east_m / (111_320.0 * max(0.1, math.cos(math.radians(lat))))
        return (lat + dlat, lon + dlon, accuracy)

    @property
    def current_segment_id(self) -> Optional[str]:
        """The nearest NH-348 corridor segment id (for the Redis jam lookup)."""
        seg = nearest_segment(*self.position)
        return seg.id if seg else None


def _cumulative_km(points: List[LatLon]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(points)):
        cum.append(cum[-1] + haversine_km(points[i - 1], points[i]))
    return cum
