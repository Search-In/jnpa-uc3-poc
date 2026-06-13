"""ScenarioHandle — the lifecycle + timeline object every scenario returns.

A handle is created at the start of ``run()``; each downstream action a scenario
causes calls ``handle.step(...)`` which:

  1. appends the step to the in-memory timeline,
  2. persists it to ``jnpa.scenario_steps`` (replay survives a reload),
  3. mirrors the trigger source + detail into ``jnpa.scenarios.params.steps[]``
     (the reactive-workflow audit the spec requires),
  4. fans it out to dashboard WS clients via the gateway
     ``POST /api/scenario_step`` (``type=scenario_step``).

Every step records its ``trigger`` source so the audit shows what caused it, and
``handle.step`` is safe to call repeatedly (steps are append-only, numbered).
The handle also carries the W3C ``traceparent`` so the dashboard can deep-link
the whole run to a Jaeger trace.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from jnpa_shared.logging import get_logger
from jnpa_shared import tracing

from .config import ScenarioConfig

log = get_logger("scenarios.handle")


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def new_handle_id(name: str) -> str:
    # Monotonic-ish, RNG-free id (Math.random/Date unavailable in some runners,
    # but time.time on the server is fine). Stable enough for a PoC handle.
    return f"{name}-{int(time.time() * 1000)}"


@dataclass
class Step:
    step_no: int
    ts: str
    title: str
    status: str
    trigger: Optional[str]
    detail: Dict[str, Any]


@dataclass
class ScenarioHandle:
    handle_id: str
    name: str
    params: Dict[str, Any]
    cfg: ScenarioConfig
    trace_id: Optional[str] = None
    status: str = "RUNNING"
    started_at: str = field(default_factory=lambda: _now().isoformat())
    ended_at: Optional[str] = None
    steps: List[Step] = field(default_factory=list)
    # Resources to undo on reset (filled by the scenario as it acts).
    cleanup: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ steps
    async def step(
        self,
        title: str,
        *,
        trigger: Optional[str] = None,
        status: str = "ok",
        detail: Optional[Dict[str, Any]] = None,
    ) -> Step:
        s = Step(
            step_no=len(self.steps) + 1,
            ts=_now().isoformat(),
            title=title,
            status=status,
            trigger=trigger,
            detail=detail or {},
        )
        self.steps.append(s)
        log.info("scenario_step", handle=self.handle_id, step=s.step_no,
                 title=title, status=status, trigger=trigger)
        await self._persist_step(s)
        await self._broadcast_step(s)
        return s

    async def _persist_step(self, s: Step) -> None:
        from jnpa_shared.db import execute
        import json
        try:
            await execute(
                """
                INSERT INTO jnpa.scenario_steps
                    (handle_id, step_no, ts, title, status, trigger, detail)
                VALUES (:hid, :no, :ts, :title, :status, :trigger, CAST(:detail AS jsonb))
                """,
                {"hid": self.handle_id, "no": s.step_no, "ts": s.ts, "title": s.title,
                 "status": s.status, "trigger": s.trigger, "detail": json.dumps(s.detail)},
                dsn=self.cfg.postgres_dsn,
            )
            # Mirror into scenarios.params.steps[] (audit).
            await execute(
                """
                UPDATE jnpa.scenarios
                SET params = jsonb_set(
                    coalesce(params, '{}'::jsonb), '{steps}',
                    coalesce(params->'steps', '[]'::jsonb) || CAST(:step AS jsonb), true)
                WHERE id = :hid
                """,
                {"hid": self.handle_id,
                 "step": json.dumps({"step_no": s.step_no, "title": s.title,
                                     "status": s.status, "trigger": s.trigger, "ts": s.ts})},
                dsn=self.cfg.postgres_dsn,
            )
        except Exception as exc:  # noqa: BLE001 - timeline persistence best-effort
            log.warning("step_persist_failed", handle=self.handle_id, error=str(exc))

    async def _broadcast_step(self, s: Step) -> None:
        payload = {
            "handle_id": self.handle_id,
            "scenario": self.name,
            "step_no": s.step_no,
            "title": s.title,
            "status": s.status,
            "trigger": s.trigger,
            "ts": s.ts,
            "detail": s.detail,
            "trace_id": self.trace_id,
        }
        url = self.cfg.gateway_url.rstrip("/") + "/api/scenario_step"
        try:
            async with httpx.AsyncClient(timeout=self.cfg.upstream_timeout_s) as c:
                await c.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.debug("step_broadcast_failed", error=str(exc))

    # --------------------------------------------------------------- lifecycle
    async def create_row(self) -> None:
        """Insert the scenarios + scenario_handles rows for this run."""
        from jnpa_shared.db import execute
        import json
        self.trace_id = tracing.current_traceparent()
        try:
            await execute(
                """
                INSERT INTO jnpa.scenarios (id, name, started_at, params)
                VALUES (:id, :name, now(), CAST(:params AS jsonb))
                ON CONFLICT (id) DO UPDATE SET started_at = now(), params = EXCLUDED.params
                """,
                {"id": self.handle_id, "name": self.name,
                 "params": json.dumps({"params": self.params, "steps": []})},
                dsn=self.cfg.postgres_dsn,
            )
            await execute(
                """
                INSERT INTO jnpa.scenario_handles
                    (handle_id, name, status, params, trace_id, started_at)
                VALUES (:hid, :name, 'RUNNING', CAST(:params AS jsonb), :trace, now())
                ON CONFLICT (handle_id) DO NOTHING
                """,
                {"hid": self.handle_id, "name": self.name,
                 "params": json.dumps(self.params), "trace": self.trace_id},
                dsn=self.cfg.postgres_dsn,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("handle_create_failed", handle=self.handle_id, error=str(exc))

    async def finish(self, status: str = "DONE") -> None:
        self.status = status
        self.ended_at = _now().isoformat()
        from jnpa_shared.db import execute
        try:
            await execute(
                "UPDATE jnpa.scenario_handles SET status = :s, ended_at = now() WHERE handle_id = :hid",
                {"s": status, "hid": self.handle_id}, dsn=self.cfg.postgres_dsn,
            )
            if status in ("DONE", "RESET"):
                await execute(
                    "UPDATE jnpa.scenarios SET ended_at = now() WHERE id = :hid",
                    {"hid": self.handle_id}, dsn=self.cfg.postgres_dsn,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("handle_finish_failed", handle=self.handle_id, error=str(exc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "name": self.name,
            "status": self.status,
            "params": self.params,
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "steps": [s.__dict__ for s in self.steps],
            "cleanup": self.cleanup,
        }


__all__ = ["ScenarioHandle", "Step", "new_handle_id"]
