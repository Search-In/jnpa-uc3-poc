"""scenarios-runner — the scheduler service (port 8400, Sub-Criterion 5).

    POST /scenarios/{name}/run    -> {handle_id, ...}   (runs the scenario chain)
    POST /scenarios/{name}/reset  -> {ok}               (resets the latest run,
                                                          or a specific handle_id)
    GET  /scenarios/{handle_id}/timeline -> event-by-event log (from Postgres)
    GET  /scenarios               -> list registered scenarios + running handles
    GET  /healthz                 -> liveness

Every scenario also emits OpenTelemetry traces (jnpa_shared.tracing) so the chain
ingest -> AI -> alert -> action shows up in Jaeger, and every step is pushed to
/api/ws (type=scenario_step) via the gateway so the dashboard paints the
storyline live.

Run with ``scenarios-runner`` (console script) or ``uvicorn scenarios.runner:app``.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException

from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared import tracing

from . import get_scenario, scenario_names
from .config import ScenarioConfig
from .handle import ScenarioHandle, new_handle_id

cfg = ScenarioConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("scenarios.runner")

tracing.init_tracing(os.environ.get("OTEL_SERVICE_NAME", "scenarios-runner"))
tracing.instrument_httpx()

# In-memory live handles (handle_id -> ScenarioHandle) so reset has the cleanup
# context for the current process. The timeline endpoint reads Postgres, so a
# restart still serves history (just can't reset a pre-restart run's resources).
_HANDLES: Dict[str, ScenarioHandle] = {}


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    log.info("scenarios_runner_ready", port=cfg.port, scenarios=scenario_names())
    yield


app = FastAPI(title="JNPA UC-III Scenarios Runner", version="0.1.0", lifespan=_lifespan)
tracing.instrument_fastapi(app)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": "scenarios-runner",
            "scenarios": scenario_names(), "running": list(_HANDLES.keys())}


@app.get("/scenarios")
async def list_scenarios() -> dict:
    return {
        "scenarios": scenario_names(),
        "handles": [h.to_dict() for h in _HANDLES.values()],
    }


@app.post("/scenarios/{name}/run")
async def run_scenario(name: str, params: Dict[str, Any] = Body(default_factory=dict)) -> dict:
    module = get_scenario(name)
    if module is None:
        raise HTTPException(status_code=404,
                            detail={"error": "unknown_scenario", "name": name,
                                    "known": scenario_names()})
    # A full run takes minutes (truck injection, forecaster polling, advisories).
    # Run it in the BACKGROUND and return the handle immediately — otherwise the
    # gateway's proxy call blocks for the whole run and times out, surfacing as
    # 502 "scenarios_runner_unreachable" even though the scenario completes. The
    # dashboard tracks progress live via the scenario_step WS frames + timeline.
    handle_id = new_handle_id(name)

    async def _execute() -> None:
        try:
            with tracing.span(f"scenario.{name}.request", {"scenario": name}):
                handle = await module.run(params or {}, handle_id=handle_id)
            _HANDLES[handle.handle_id] = handle
        except Exception as exc:  # noqa: BLE001 - background task must not crash the loop
            log.warning("scenario_run_failed", name=name, handle_id=handle_id, error=str(exc))

    asyncio.create_task(_execute())
    return {"handle_id": handle_id, "name": name, "status": "RUNNING", "steps": 0}


@app.post("/scenarios/{name}/reset")
async def reset_scenario(
    name: str, body: Dict[str, Any] = Body(default_factory=dict)
) -> dict:
    """Reset a scenario. Body may carry ``{handle_id}``; otherwise the most recent
    running handle for ``name`` is reset."""
    handle_id = body.get("handle_id")
    handle = _resolve_handle(name, handle_id)
    if handle is None:
        # Nothing live to reset (e.g. after a runner restart). Best-effort: try a
        # fresh module reset against a synthetic handle so DB/sim cleanup by tag
        # still runs if the caller passes a handle_id.
        if handle_id:
            module = get_scenario(name)
            if module is not None:
                stub = ScenarioHandle(handle_id=handle_id, name=name, params={}, cfg=cfg,
                                      cleanup={"truck_tag": f"{name.upper()}:{handle_id}"})
                await module.reset(stub)
                return {"ok": True, "handle_id": handle_id, "note": "reset via stub (post-restart)"}
        raise HTTPException(status_code=404,
                            detail={"error": "no_running_handle", "name": name})
    module = get_scenario(name)
    await module.reset(handle)
    return {"ok": True, "handle_id": handle.handle_id, "status": handle.status}


@app.get("/scenarios/{handle_id}/timeline")
async def timeline(handle_id: str) -> dict:
    """Event-by-event log for a run, read from Postgres (survives a reload)."""
    from jnpa_shared.db import fetch_all, fetch_one
    head = await fetch_one(
        "SELECT handle_id, name, status, trace_id, started_at, ended_at, params "
        "FROM core.scenario_handle WHERE handle_id = :hid",
        {"hid": handle_id}, dsn=cfg.postgres_dsn,
    )
    rows = await fetch_all(
        "SELECT step_no, ts, title, status, trigger, detail FROM core.scenario_step "
        "WHERE handle_id = :hid ORDER BY step_no",
        {"hid": handle_id}, dsn=cfg.postgres_dsn,
    )
    if not head and not rows:
        raise HTTPException(status_code=404, detail={"error": "unknown_handle", "handle_id": handle_id})
    steps = []
    for r in rows:
        d = dict(r)
        for k in ("ts",):
            if d.get(k) is not None and not isinstance(d[k], str):
                d[k] = d[k].isoformat()
        steps.append(d)
    out: Dict[str, Any] = {"handle_id": handle_id, "steps": steps, "count": len(steps)}
    if head:
        hd = dict(head)
        for k in ("started_at", "ended_at"):
            if hd.get(k) is not None and not isinstance(hd[k], str):
                hd[k] = hd[k].isoformat()
        out.update(hd)
    return out


def _resolve_handle(name: str, handle_id: Optional[str]) -> Optional[ScenarioHandle]:
    if handle_id:
        return _HANDLES.get(handle_id)
    candidates = [h for h in _HANDLES.values() if h.name == name.lower()]
    if not candidates:
        return None
    return sorted(candidates, key=lambda h: h.started_at)[-1]


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
