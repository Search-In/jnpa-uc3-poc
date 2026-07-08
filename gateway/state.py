"""Shared gateway application state.

A single ``GatewayState`` is built in the FastAPI lifespan and stashed on
``app.state.gw``. Routers reach it via the ``get_state`` dependency. It owns:

* the typed ``GatewayConfig``
* a pooled ``httpx.AsyncClient`` for upstream proxying
* the ``DecisionRing`` (last 1000 decisions) + ``SourceRegistry`` (health table)
* the ``WsHub`` for the /api/ws fan-out

``record_decision`` is the one funnel every orchestrated path calls: it stamps
the decision, logs it structured with ``decision_path=``, bumps Prometheus,
pushes it on the ring buffer, updates source health, and — when a fallback rung
below the primary fired — broadcasts a ``type=decision`` frame to WS clients.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import httpx
from fastapi import Request

from . import audit
from .audit_client import AuditingAsyncClient
from .config import GatewayConfig
from .geofence import GeofenceEngine
from .fallback import (
    DecisionPath,
    DecisionRing,
    FaultRegistry,
    SourceHealth,
    SourceRegistry,
    SourceState,
)
from .logging import get_logger
from .metrics import DECISIONS, SOURCE_STATE
from .ws import WsHub

log = get_logger("gateway.state")

_SEVERITY_RANK = {"GREEN": 0, "AMBER": 1, "RED": 2}


def _max_severity(domain_values) -> Optional[str]:
    """Highest severity (RED > AMBER > GREEN) across the active fault domains."""
    sev = [v["severity"] for v in domain_values if v.get("severity")]
    return max(sev, key=lambda s: _SEVERITY_RANK.get(s, 0)) if sev else None


class GatewayState:
    def __init__(self, cfg: GatewayConfig) -> None:
        self.cfg = cfg
        # Auditing HTTP client: every outbound (external) call is logged to
        # jnpa.api_audit_log (request/response/status/latency/error). Drop-in for
        # httpx.AsyncClient — behaviour is unchanged, logging is fire-and-forget.
        self.http = AuditingAsyncClient(
            timeout=cfg.upstream_timeout_s, audit_dsn=cfg.postgres_dsn or None
        )
        self.decisions = DecisionRing(maxlen=cfg.decision_ring_size)
        self.sources = SourceRegistry()
        # Presenter-controllable fault injection (POST /api/control/fault/...).
        # Read at the top of each fallback chain to force a rung on demand.
        self.faults = FaultRegistry()
        self.ws = WsHub()
        # DB-driven geo-fence enforcement engine (reads jnpa.geofence_zones live).
        # Fed by the MQTT truck pump + POST /api/geo/evaluate (mobile location).
        self.geofence = GeofenceEngine(cfg.postgres_dsn or None)

    async def aclose(self) -> None:
        await self.http.aclose()

    # ----------------------------------------------------------------- faults
    async def broadcast_operator_banner(self) -> dict:
        """Push the current fault state as an ``operator_banner`` WS frame.

        The banner payload lists every domain's forced rung + severity so the
        dashboard can flip the corresponding Health Card and raise the banner.
        Returns the payload (also used as the control-endpoint response body).
        """
        snap = self.faults.snapshot()
        active = {d: v for d, v in snap.items() if v["forced_rung"]}
        payload = {
            "active": bool(active),
            "domains": snap,
            "severity": _max_severity(active.values()),
        }
        await self.ws.broadcast("operator_banner", payload)
        return payload

    # ---------------------------------------------------------------- decisions
    async def record_decision(
        self,
        *,
        api: str,
        decision_path: str,
        key: Optional[str] = None,
        latency_ms: Optional[float] = None,
        detail: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
        source_state: Optional[SourceState] = None,
        ok: bool = True,
    ) -> DecisionPath:
        """Funnel every orchestrated decision through here (audit + telemetry)."""
        decision = DecisionPath(
            api=api,
            key=key,
            decision_path=decision_path,
            latency_ms=latency_ms,
            detail=detail or {},
        )
        self.decisions.add(decision)
        DECISIONS.labels(api, decision_path).inc()

        # Durable audit trail (replaces reliance on the in-memory ring). Persisted
        # fire-and-forget so the decision path is never slowed by the DB write.
        audit.spawn(
            audit.record_decision_audit(
                request_id=key,
                input_data={"api": api, "source": source, "detail": detail or {}},
                rule_executed=api,
                decision=decision_path,
                action_taken="FALLBACK" if decision.is_fallback else "PRIMARY",
            )
        )

        # Update per-source health (defaults: source == api, state inferred).
        src = source or api
        state = source_state or (
            SourceState.LIVE if not decision.is_fallback else SourceState.DEGRADED
        )
        self.sources.observe(
            src, state=state, latency_ms=latency_ms,
            decision_path=decision_path, ok=ok,
        )
        SOURCE_STATE.labels(src).set(self.sources.gauge_value(src))

        log.info(
            "decision",
            api=api,
            key=key,
            decision_path=decision_path,
            latency_ms=round(latency_ms, 1) if latency_ms is not None else None,
            fallback=decision.is_fallback,
            **(detail or {}),
        )

        # Surface the fallback to live dashboards (spec: type=decision on WS,
        # only when fallback fires).
        if decision.is_fallback:
            await self.ws.broadcast("decision", decision.model_dump(mode="json"))
        return decision

    def observe_source(
        self, source: str, *, state: SourceState, latency_ms: Optional[float] = None,
        decision_path: Optional[str] = None, ok: bool = True,
    ) -> None:
        self.sources.observe(
            source, state=state, latency_ms=latency_ms,
            decision_path=decision_path, ok=ok,
        )
        SOURCE_STATE.labels(source).set(self.sources.gauge_value(source))


def get_state(request: Request) -> GatewayState:
    """FastAPI dependency: pull the GatewayState off app.state."""
    return request.app.state.gw


__all__ = ["GatewayState", "get_state"]
