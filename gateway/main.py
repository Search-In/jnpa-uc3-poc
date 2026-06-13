"""JNPA UC-III API gateway (Sub-Criterion 3).

A single public-facing FastAPI service (port 8000) that the dashboard and the
trucking-app PWA talk to. It encodes the fallback orchestration the bid spec
requires and is the only service exposed outside the jnpa network.

Mounted routers:

    /api/anpr      -> proxy to ai/anpr + camera-feed fallback (LIVE/CACHED/SYNTHETIC)
    /api/vahan     -> orchestrated RC/DL/FASTag (LIVE_PRIMARY/LIVE_FALLBACK/CACHED/PROVISIONAL)
    /api/traffic   -> orchestrated congestion (LIVE/CACHED/SYNTHETIC)
    /api/trucks    -> trucking-app position (PRIMARY/SECONDARY/TERTIARY)
    /api/ulip      -> ULIP relay proxy (SECONDARY source; mock if no key)
    /api/alerts    -> ai/anomaly alerts (degrades to jnpa.alerts)
    /api/scenarios -> scenario driver (Prompt 9; degrades to jnpa.scenarios)
    /api/kpi       -> materialised KPI views + System-Health + camera degradation
    /api/debug     -> last 1000 fallback decisions (demo evidence)
    /api/ws        -> WebSocket fan-out (alert / traffic / truck_position / decision)
    /checkin       -> TERTIARY manual check-in form
    /metrics       -> Prometheus exposition
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from jnpa_shared.schemas import TOPIC_ALERTS, TOPIC_TRAFFIC

from .config import GatewayConfig
from .logging import configure_logging, get_logger
from .metrics import metrics_asgi_app
from .pumps import KafkaPump, mqtt_truck_pump
from .routers import (
    alerts,
    anpr,
    checkin,
    debug,
    geo,
    kpi,
    reports,
    scenarios,
    traffic,
    trucks,
    ulip,
    vahan,
    ws,
)
from .state import GatewayState

cfg = GatewayConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("gateway")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    state = GatewayState(cfg)
    app.state.gw = state
    log.info("gateway_starting", port=cfg.port, surepass_enabled=cfg.surepass_enabled)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    # Kafka pumps (blocking consumer threads) — best-effort.
    alert_pump = KafkaPump(state, loop, TOPIC_ALERTS, "alert", "jnpa-gateway-alerts")
    traffic_pump = KafkaPump(state, loop, TOPIC_TRAFFIC, "traffic", "jnpa-gateway-traffic")
    alert_pump.start()
    traffic_pump.start()

    # MQTT truck-position pump (async task) — best-effort.
    mqtt_task = asyncio.create_task(mqtt_truck_pump(state, stop), name="mqtt-truck-pump")

    try:
        yield
    finally:
        stop.set()
        alert_pump.stop()
        traffic_pump.stop()
        mqtt_task.cancel()
        try:
            await mqtt_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await state.aclose()
        log.info("gateway_stopped")


app = FastAPI(
    title="JNPA UC-III API Gateway + Fallback Orchestrator",
    version="0.1.0",
    lifespan=_lifespan,
)

# The dashboard + PWA are browser clients on other origins; allow them.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers (order matters only where static paths must beat /{param} — kpi router
# declares /sources + /cameras before /{view}, so it is safe).
app.include_router(anpr.router)
app.include_router(vahan.router)
app.include_router(traffic.router)
app.include_router(trucks.router)
app.include_router(ulip.router)
app.include_router(alerts.router)
app.include_router(scenarios.router)
app.include_router(kpi.router)
app.include_router(geo.router)
app.include_router(reports.router)
app.include_router(debug.router)
app.include_router(ws.router)
app.include_router(checkin.router)

app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": "jnpa-gateway",
        "surepass_enabled": cfg.surepass_enabled,
        "ws_clients": app.state.gw.ws.client_count if hasattr(app.state, "gw") else 0,
    }


@app.get("/")
async def root() -> dict:
    return {
        "service": "JNPA UC-III API Gateway",
        "version": "0.1.0",
        "apis": ["/api/anpr", "/api/vahan", "/api/traffic", "/api/trucks",
                 "/api/ulip", "/api/alerts", "/api/scenarios", "/api/kpi",
                 "/api/gates", "/api/corridor", "/api/zones",
                 "/api/reports/police", "/api/debug/decisions", "/api/ws",
                 "/checkin"],
    }


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
