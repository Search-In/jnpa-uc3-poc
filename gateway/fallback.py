"""Fallback orchestrator — the decision engine behind Sub-Criterion 3.

This module is deliberately transport-agnostic: the routers (``routers/*.py``)
own the HTTP plumbing and call into the small primitives here so the *decision*
logic — which fallback rung served a request — lives in one auditable place.

Three fallback chains are encoded (matching the bid spec):

1. Camera / ANPR feed:
       LIVE       -> ingest/anpr healthy AND < 2 s lag
       CACHED     -> last 60 s of frames replayed from a Redis Stream
       SYNTHETIC  -> synthetic plate generator (text overlaid on a stock frame)

2. Vahan / Sarathi / FastTag:
       LIVE_PRIMARY  -> vahan-live (only if SUREPASS_API_TOKEN is set)
       LIVE_FALLBACK -> vahan-sim
       CACHED        -> last response from Redis (TTL 12 h)
       PROVISIONAL   -> admit with provisional=true + a 24 h cure window;
                        write jnpa.vehicle_master(provisional_until=now()+24h)
                        and emit Alert(kind=PROVISIONAL_VEHICLE).

3. Trucking App:
       PRIMARY   -> trucking-app GPS via MQTT trucks/+/telemetry
       SECONDARY -> ULIP relay GPS via /api/ulip/proxy (mock if no key)
       TERTIARY  -> web check-in form at /checkin
       (elevated scrutiny: Alert(kind=ELEVATED_SCRUTINY), gate boom delay +5 s)

Every decision is recorded as a structured ``DecisionPath`` with a
``decision_path`` field, pushed onto a bounded ring buffer (demo evidence for
``/api/debug/decisions``) and broadcast to WebSocket clients when a fallback
rung below the primary fired.
"""
from __future__ import annotations

import collections
import enum
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Decision-path vocabularies (string enums so they serialise as plain strings)
# ---------------------------------------------------------------------------
class AnprPath(str, enum.Enum):
    LIVE = "LIVE"
    CACHED = "CACHED"
    SYNTHETIC = "SYNTHETIC"


class VahanPath(str, enum.Enum):
    LIVE_PRIMARY = "LIVE_PRIMARY"
    LIVE_FALLBACK = "LIVE_FALLBACK"
    CACHED = "CACHED"
    PROVISIONAL = "PROVISIONAL"


class TruckPath(str, enum.Enum):
    PRIMARY = "PRIMARY"
    SECONDARY = "SECONDARY"
    TERTIARY = "TERTIARY"


# Which paths count as "the primary, all-healthy rung" per API. Anything other
# than these is a *fallback* and is broadcast on the WebSocket decision channel.
PRIMARY_PATHS = {
    "anpr": AnprPath.LIVE.value,
    "vahan": VahanPath.LIVE_PRIMARY.value,
    "traffic": "LIVE",
    "trucks": TruckPath.PRIMARY.value,
}


class DecisionPath(BaseModel):
    """One orchestration decision — the demo's audit record + WS payload."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(default_factory=lambda: uuid4().hex)
    ts: datetime = Field(default_factory=_utcnow)
    api: str                              # "vahan" | "anpr" | "traffic" | "trucks"
    key: Optional[str] = None             # e.g. the plate / camera / segment
    decision_path: str                    # the rung that served the request
    latency_ms: Optional[float] = None
    detail: Dict[str, Any] = Field(default_factory=dict)

    @property
    def is_fallback(self) -> bool:
        """True if a rung *below* the all-healthy primary served this call."""
        primary = PRIMARY_PATHS.get(self.api)
        return primary is not None and self.decision_path != primary


# ---------------------------------------------------------------------------
# Decision ring buffer — last N decisions, demo evidence for /api/debug
# ---------------------------------------------------------------------------
class DecisionRing:
    """Bounded in-memory ring buffer of the most recent DecisionPath records.

    Newest-first iteration so ``/api/debug/decisions`` returns the latest call
    at index 0 (matches the verification command ``jq '.[0]'``).
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._dq: Deque[DecisionPath] = collections.deque(maxlen=maxlen)

    def add(self, decision: DecisionPath) -> None:
        self._dq.append(decision)

    def recent(self, limit: Optional[int] = None) -> List[DecisionPath]:
        items = list(reversed(self._dq))   # newest first
        return items[:limit] if limit else items

    def __len__(self) -> int:
        return len(self._dq)

    def clear(self) -> None:
        self._dq.clear()


# ---------------------------------------------------------------------------
# Source-health registry — backs /api/kpi/sources ("System Health" panel)
# ---------------------------------------------------------------------------
class SourceState(str, enum.Enum):
    LIVE = "LIVE"            # serving from the primary upstream
    DEGRADED = "DEGRADED"    # serving from a fallback rung (cache/sim/relay)
    DOWN = "DOWN"            # nothing answered; provisional / synthetic only


_STATE_GAUGE_VALUE = {SourceState.LIVE: 0, SourceState.DEGRADED: 1, SourceState.DOWN: 2}


class SourceHealth(BaseModel):
    """One row of the {source, state, last_ok, latency_p95} health table."""

    model_config = ConfigDict(extra="ignore")

    source: str
    state: SourceState = SourceState.LIVE
    last_ok: Optional[datetime] = None
    latency_p95_ms: Optional[float] = None
    last_decision_path: Optional[str] = None


class SourceRegistry:
    """Tracks per-source state + a rolling latency window for p95."""

    def __init__(self, window: int = 200) -> None:
        self._sources: Dict[str, SourceHealth] = {}
        self._latencies: Dict[str, Deque[float]] = {}
        self._window = window

    def observe(
        self,
        source: str,
        *,
        state: SourceState,
        latency_ms: Optional[float] = None,
        decision_path: Optional[str] = None,
        ok: bool = True,
    ) -> None:
        health = self._sources.get(source) or SourceHealth(source=source)
        health.state = state
        health.last_decision_path = decision_path
        if ok:
            health.last_ok = _utcnow()
        if latency_ms is not None:
            dq = self._latencies.setdefault(source, collections.deque(maxlen=self._window))
            dq.append(latency_ms)
            health.latency_p95_ms = _p95(dq)
        self._sources[source] = health

    def table(self) -> List[SourceHealth]:
        return list(self._sources.values())

    def gauge_value(self, source: str) -> int:
        h = self._sources.get(source)
        return _STATE_GAUGE_VALUE[h.state] if h else 2


def _p95(values: Deque[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, int(0.95 * len(ordered)) - 1)
    return round(ordered[idx], 2)


# ---------------------------------------------------------------------------
# Fault injection — presenter-controllable forcing of a fallback rung
# ---------------------------------------------------------------------------
# A "fault domain" is one of the three chains. Forcing a domain pins every
# decision in that chain to the named rung, so a presenter can make
# "PROVISIONAL 24-hr cure" or "GPS -> ULIP -> web-checkin" a live click rather
# than words in the bid. The override is in-memory (per gateway process) and is
# read at the TOP of each chain's decision function, short-circuiting the normal
# health cascade. Clearing a domain resumes the real cascade.
FAULT_DOMAINS = ("camera", "vahan", "trucks")

# Allowed forced rungs per domain (validated by the control endpoint). The
# values match the path enums above so a forced rung is a real decision_path.
FAULT_RUNGS: Dict[str, tuple] = {
    "camera": (AnprPath.LIVE.value, AnprPath.CACHED.value, AnprPath.SYNTHETIC.value),
    "vahan": (
        VahanPath.LIVE_PRIMARY.value, VahanPath.LIVE_FALLBACK.value,
        VahanPath.CACHED.value, VahanPath.PROVISIONAL.value,
    ),
    "trucks": (
        TruckPath.PRIMARY.value, TruckPath.SECONDARY.value, TruckPath.TERTIARY.value,
    ),
}

# Health-Card colour for the forced rung (AMBER = degraded fallback, RED = the
# last-ditch rung). Drives the Operator Banner severity.
_RUNG_SEVERITY = {
    AnprPath.LIVE.value: "GREEN", AnprPath.CACHED.value: "AMBER",
    AnprPath.SYNTHETIC.value: "RED",
    VahanPath.LIVE_PRIMARY.value: "GREEN", VahanPath.LIVE_FALLBACK.value: "GREEN",
    VahanPath.CACHED.value: "AMBER", VahanPath.PROVISIONAL.value: "RED",
    TruckPath.PRIMARY.value: "GREEN", TruckPath.SECONDARY.value: "AMBER",
    TruckPath.TERTIARY.value: "RED",
}


class FaultRegistry:
    """In-memory forced-rung overrides, one per fault domain.

    ``force(domain, rung)`` pins the domain; ``clear(domain)`` releases it;
    ``forced(domain)`` returns the pinned rung or ``None`` (no override → run the
    real cascade). All reads are O(1) and side-effect-free so they're cheap to
    call at the top of every request.
    """

    def __init__(self) -> None:
        self._forced: Dict[str, str] = {}

    def force(self, domain: str, rung: str) -> None:
        if domain not in FAULT_DOMAINS:
            raise ValueError(f"unknown fault domain: {domain}")
        if rung not in FAULT_RUNGS[domain]:
            raise ValueError(f"rung {rung!r} not valid for domain {domain!r}")
        self._forced[domain] = rung

    def clear(self, domain: str) -> None:
        self._forced.pop(domain, None)

    def clear_all(self) -> None:
        self._forced.clear()

    def forced(self, domain: str) -> Optional[str]:
        return self._forced.get(domain)

    def severity(self, domain: str) -> Optional[str]:
        rung = self._forced.get(domain)
        return _RUNG_SEVERITY.get(rung) if rung else None

    def snapshot(self) -> Dict[str, Dict[str, Optional[str]]]:
        """{domain: {rung, severity}} for every domain (forced or not)."""
        out: Dict[str, Dict[str, Optional[str]]] = {}
        for d in FAULT_DOMAINS:
            rung = self._forced.get(d)
            out[d] = {"forced_rung": rung, "severity": _RUNG_SEVERITY.get(rung) if rung else None}
        return out


__all__ = [
    "AnprPath",
    "VahanPath",
    "TruckPath",
    "PRIMARY_PATHS",
    "DecisionPath",
    "DecisionRing",
    "SourceState",
    "SourceHealth",
    "SourceRegistry",
    "FaultRegistry",
    "FAULT_DOMAINS",
    "FAULT_RUNGS",
]
