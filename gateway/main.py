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

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .mode import ProductionSafetyError, mode_name, production_mode

from jnpa_shared.schemas import TOPIC_ALERTS, TOPIC_ANPR, TOPIC_TRAFFIC
from jnpa_shared import tracing

from . import audit
from .config import GatewayConfig
from .logging import configure_logging, get_logger
from .metrics import metrics_asgi_app
from .pumps import KafkaPump, mqtt_truck_pump
from .auth import install_auth, validate_auth_config
from .routers import (
    ai_events,
    alerts,
    anpr,
    auth as auth_router,
    carbon,
    checkin,
    control,
    debug,
    empty_container,
    evidence,
    fastag,
    gate_data,
    geo,
    identity,
    kpi,
    otp,
    parking,
    push,
    reports,
    scenario_ext,
    scenarios,
    traffic,
    trucks,
    ulip,
    vahan,
    violations,
    ws,
)
from .state import GatewayState

cfg = GatewayConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("gateway")

# Fail fast on an unsafe auth posture BEFORE the app is constructed or any port is
# bound: staging/production must run with AUTH_ENABLED=true, a non-default
# AUTH_JWT_SECRET, and the dev-token seam disabled (C1/C2/C3). A no-op for a
# correctly configured deployment and for local development. Raising here aborts
# process startup with a clear, actionable message.
validate_auth_config()

# OpenTelemetry: export spans to Jaeger (no-op if otel deps / endpoint absent).
# instrument_httpx() makes the gateway's outbound proxy calls continue the trace
# so the causal chain (dashboard -> gateway -> upstream AI/sim) nests in Jaeger.
tracing.init_tracing(__import__("os").environ.get("OTEL_SERVICE_NAME", "gateway"))
tracing.instrument_httpx()


from . import enrollment, objectstore


async def _readiness(state: "GatewayState") -> tuple[bool, dict]:
    """Production readiness of the gateway's REQUIRED dependencies.

    Postgres (enrolment/audit store) and MinIO (reference-photo store) must both be
    reachable; the identity service must answer /healthz READY. In development the
    gateway is always READY (fallbacks are allowed). Drives the startup gate AND
    ``/healthz``."""
    if not production_mode():
        return True, {"mode": "development"}
    checks: dict = {}
    try:
        await enrollment.ensure_backend(cfg.postgres_dsn)
        checks["postgres"] = True
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = False
        checks["postgres_detail"] = str(exc)
    minio_ok, minio_detail = objectstore.healthcheck()
    checks["minio"] = minio_ok
    if not minio_ok:
        checks["minio_detail"] = minio_detail
    # Identity service (ArcFace/liveness) must report READY.
    try:
        resp = await state.http.get(cfg.identity_url.rstrip("/") + "/healthz", timeout=5.0)
        checks["identity"] = resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        checks["identity"] = False
        checks["identity_detail"] = str(exc)
    ok = bool(checks.get("postgres") and checks.get("minio") and checks.get("identity"))
    return ok, checks


async def _production_startup_gate(state: "GatewayState") -> None:
    """FAIL FAST: in production refuse to start unless Postgres + MinIO are up.

    (The identity service guards its own ArcFace/liveness models on its boot.) A
    no-op in development. Raised from the lifespan so uvicorn aborts the boot."""
    if not production_mode():
        return
    await enrollment.ensure_backend(cfg.postgres_dsn)  # raises ProductionSafetyError if down
    minio_ok, minio_detail = objectstore.healthcheck()
    if not minio_ok:
        raise ProductionSafetyError("minio", minio_detail)
    log.info("gateway_production_dependencies_ready", postgres=True, minio=True)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    state = GatewayState(cfg)
    app.state.gw = state
    log.info("gateway_starting", port=cfg.port, surepass_enabled=cfg.surepass_enabled)

    # FAIL FAST: a missing Postgres/MinIO in production aborts the boot.
    await _production_startup_gate(state)

    # Apply the idempotent audit/event DDL + register the default DSN the
    # fire-and-forget writers use. Best-effort: a DB blip never aborts boot.
    audit.configure(cfg.postgres_dsn or None)
    try:
        await audit.ensure_audit_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("audit_schema_boot_failed", error=str(exc))

    # Geo-fence enforcement engine: ensure event columns + warm the DB zone cache.
    try:
        await state.geofence.ensure_schema()
        n = await state.geofence.refresh_zones(force=True)
        log.info("geofence_engine_ready", zones=n)
    except Exception as exc:  # noqa: BLE001
        log.warning("geofence_boot_failed", error=str(exc))

    # Vehicle/Driver intelligence history tables (Vahan/Sarathi).
    try:
        from . import vehicle_intel
        await vehicle_intel.ensure_intel_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("intel_schema_boot_failed", error=str(exc))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    # Kafka pumps (blocking consumer threads) — best-effort. The alert pump ALSO
    # mirrors every alert into jnpa.digital_twin_events (+ geofence_events for
    # zone-family kinds); a persistence-only pump lands ANPR reads in
    # jnpa.anpr_reads (finally giving that table its writer) + the event timeline.
    alert_pump = KafkaPump(
        state, loop, TOPIC_ALERTS, "alert", "jnpa-gateway-alerts",
        persist=audit.persist_alert_event,
    )
    traffic_pump = KafkaPump(state, loop, TOPIC_TRAFFIC, "traffic", "jnpa-gateway-traffic")
    anpr_pump = KafkaPump(
        state, loop, TOPIC_ANPR, "anpr", "jnpa-gateway-anpr",
        persist=audit.persist_anpr_read, broadcast=False,
    )
    alert_pump.start()
    traffic_pump.start()
    anpr_pump.start()

    # MQTT truck-position pump (async task) — best-effort.
    mqtt_task = asyncio.create_task(mqtt_truck_pump(state, stop), name="mqtt-truck-pump")

    try:
        yield
    finally:
        stop.set()
        alert_pump.stop()
        traffic_pump.stop()
        anpr_pump.stop()
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
tracing.instrument_fastapi(app)

# The dashboard + PWA are browser clients on other origins. CORS is origin-scoped
# from env in production (CORS_ALLOW_ORIGINS="https://dash.jnpa,https://pwa.jnpa");
# the default "*" keeps local/mock dev frictionless. Setting explicit origins also
# enables credentialed requests (cookies/Authorization) which "*" forbids.
import os as _os

_origins_env = _os.environ.get("CORS_ALLOW_ORIGINS", "").strip()
_allow_origins = [o.strip() for o in _origins_env.split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Auth + RBAC + rate-limit gate. Flag-gated: pass-through unless AUTH_ENABLED=true
# (so the demo/mock profile and the in-process test suite are unaffected), full
# JWT-bearer + per-path role enforcement when on. See gateway/auth.py.
install_auth(app)


# Structured 503 when a REQUIRED production dependency (Postgres / MinIO / identity)
# is unavailable — fail loud and safe instead of silently degrading. In development
# these raise paths are not taken (fallbacks are allowed by gateway/mode.py).
@app.exception_handler(ProductionSafetyError)
async def _production_safety_handler(_request: Request, exc: ProductionSafetyError):
    return JSONResponse(
        status_code=503,
        content={"error": "service_unavailable", "component": exc.component,
                 "message": str(exc), "decision_path": "UNAVAILABLE"},
    )


# The FASTag endpoints must surface request-validation failures (missing/empty
# fields, bad RC/vehicle_type, malformed JSON) as 400 — not FastAPI's default 422.
# Scoped to /api/fastag/ only; every other route keeps the default 422 behaviour.
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.exception_handlers import request_validation_exception_handler  # noqa: E402
from fastapi.encoders import jsonable_encoder  # noqa: E402


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/fastag/"):
        cid = request.headers.get("X-Correlation-ID")
        return JSONResponse(
            status_code=400,
            content={"error": "validation_error",
                     "detail": jsonable_encoder(exc.errors()),
                     "correlation_id": cid},
            headers={"X-Correlation-ID": cid} if cid else None,
        )
    return await request_validation_exception_handler(request, exc)


log.info("gateway_runtime_mode", mode=mode_name())

# Routers (order matters only where static paths must beat /{param} — kpi router
# declares /sources + /cameras before /{view}, so it is safe).
app.include_router(auth_router.router)
app.include_router(anpr.router)
app.include_router(vahan.router)
app.include_router(traffic.router)
app.include_router(trucks.router)
app.include_router(ulip.router)
app.include_router(alerts.router)
app.include_router(scenarios.router)
app.include_router(kpi.router)
app.include_router(push.router)
app.include_router(geo.router)
app.include_router(reports.router)
# Evidence proxy — streams private-MinIO evidence objects to the browser same-origin
# (so <img>/<video> load without exposing MinIO). Public (no bearer) — see router.
app.include_router(evidence.router)
# Vehicle Violation Detection — orchestration-only enforcement console. Reuses
# ANPR + vehicle_master + driver store + the reports e-Challan schedule + MinIO
# evidence and writes incidents to jnpa.alerts (so they appear on the Reports
# page). Mounted after reports because it imports its fine schedule.
app.include_router(violations.router)
# FASTag ULIP surface — /api/fastag/{balance,toll-enroute,transactions}. Thin
# router: auth+validation at the gateway, then client -> mapper -> FastagService
# (the single orchestration point). See gateway/routers/fastag.py.
app.include_router(fastag.router)
app.include_router(scenario_ext.router)
# Appendix-C capability services (Empty-Container, Carbon, Gate-Data/Auto-LEO,
# Identity/face-recognition, Parking) — each proxies its upstream and degrades
# to the service's own deterministic logic so the dashboard always renders.
app.include_router(empty_container.router)
app.include_router(carbon.router)
app.include_router(gate_data.router)
app.include_router(identity.router)
app.include_router(parking.router)
app.include_router(debug.router)
app.include_router(control.router)
app.include_router(ai_events.router)
app.include_router(otp.router)
app.include_router(ws.router)
app.include_router(checkin.router)

app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz(response: Response) -> dict:
    """READY (200) only when required dependencies are up; 503 otherwise.

    In production: Postgres, MinIO, and the identity service must all be reachable.
    In development the gateway is always READY (fallbacks allowed)."""
    state = getattr(app.state, "gw", None)
    ready, checks = (True, {"mode": "development"})
    if state is not None:
        ready, checks = await _readiness(state)
    if not ready:
        response.status_code = 503
    return {
        "status": "ready" if ready else "not_ready",
        "service": "jnpa-gateway",
        "mode": mode_name(),
        "surepass_enabled": cfg.surepass_enabled,
        "ws_clients": state.ws.client_count if state is not None else 0,
        "checks": checks,
    }


@app.get("/")
async def root() -> dict:
    return {
        "service": "JNPA UC-III API Gateway",
        "version": "0.1.0",
        "apis": ["/api/anpr", "/api/vahan", "/api/traffic", "/api/trucks",
                 "/api/ulip", "/api/alerts", "/api/scenarios", "/api/kpi",
                 "/api/gates", "/api/corridor", "/api/zones", "/api/push",
                 "/api/reports/police", "/api/empty", "/api/carbon",
                 "/api/gate-data", "/api/identity", "/api/parking",
                 "/api/debug/decisions", "/api/ws", "/checkin"],
    }


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
