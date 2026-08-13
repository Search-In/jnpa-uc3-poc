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
    /api/alerts    -> ai/anomaly alerts (degrades to core.alert)
    /api/scenarios -> scenario driver (Prompt 9; degrades to core.scenario)
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

from jnpa_shared.schemas import (TOPIC_ALERTS, TOPIC_ANPR, TOPIC_DEFERRED_ARRIVAL,
                                 TOPIC_TRAFFIC, DeferredArrivalWindow)
from jnpa_shared import tracing
from jnpa_shared.kafka_io import broker_configured as kafka_configured

from . import audit
from .config import GatewayConfig
from .logging import configure_logging, get_logger
from .metrics import metrics_asgi_app
from .pumps import KafkaPump, mqtt_truck_pump
from .auth import install_auth, validate_auth_config
from .routers import (
    export_chain,
    ai_events,
    alerts,
    anpr,
    auth as auth_router,
    carbon,
    cargo,
    cargo_simulation,
    checkin,
    control,
    debug,
    driver as driver_router,
    drivers_master,
    empty_container,
    evidence,
    fastag,
    gate_data,
    geo,
    identity,
    journey,
    kpi,
    meta,
    notifications as notifications_router,
    otp,
    parking,
    push,
    reports,
    scenario_ext,
    scenarios,
    traffic,
    trucks,
    ulip,
    users as users_router,
    vahan,
    vehicle_identity,
    vehicles,
    violations,
    workflows,
    ws,
    yard,
)
# UC-III Final-Completion routers (additive; see gateway/uc3_ext.py + migration 0024).
from .routers import (
    accidents,
    air_quality,
    berthing,
    bhuvan,
    bottlenecks,
    camera_ai,
    cfs_ecy,
    customs,
    container_job,
    document_ocr,
    double_trip,
    corridor_heatmap,
    corridor_sim,
    dq,
    driver_jobs,
    edi_vessel,
    email_processing,
    export_lifecycle,
    auto_leo,
    trip_search,
    gate_board,
    gate_documents,
    jnpa_api,
    gatishakti,
    ldb,
    logistics,
    marine_calls,
    marine_dashboard,
    marine_live_vessels,
    marine_imports,
    marine_manual_craft,
    marine_manual_pilot,
    marine_pilotage,
    marine_bathymetry,
    marine_port_craft,
    marine_sea_channel,
    marine_state,
    marine_vessel,
    nvr,
    pdp,
    performance,
    performance_upload,
    rail,
    reefer,
    rms_tas,
    securevision,
    shipping_lines,
    transporters,
    vehicle_registry,
    transporters_drivers_upload,
    trt,
    weather,
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


def _validate_environment() -> None:
    """Report environment misconfiguration at BOOT, not at first request.

    Complements ``validate_auth_config`` (which owns the fail-fast auth rules)
    with the broader required-variable sweep from ``scripts/check_env.py`` — the
    audit found nine variables the stack needs that nothing validated, including
    ``PWA_PAIRING_SECRET``, whose absence silently 401s every driver login.

    Warnings are LOGGED, never fatal: this must not become a new way for the
    gateway to refuse to start. The genuinely fatal cases are already covered by
    ``validate_auth_config`` above. Import failures are swallowed so the gateway
    still boots in a stripped image that has no scripts/ directory.
    """
    try:
        import pathlib
        import sys as _sys

        _root = str(pathlib.Path(__file__).resolve().parents[1])
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from scripts.check_env import env_source, validate  # type: ignore

        errors, warnings = validate(env_source())
        for e in errors:
            log.error("env_config_error", detail=e)
        for w in warnings:
            log.warning("env_config_warning", detail=w)
        if errors:
            log.error(
                "env_config_incomplete",
                errors=len(errors),
                hint="run `make env-check` for the full report",
            )
        else:
            log.info("env_config_ok", warnings=len(warnings))
    except Exception as exc:  # noqa: BLE001 — diagnostics must never block boot
        log.debug("env_validation_skipped", error=str(exc))


_validate_environment()

# OpenTelemetry: export spans to Jaeger (no-op if otel deps / endpoint absent).
# instrument_httpx() makes the gateway's outbound proxy calls continue the trace
# so the causal chain (dashboard -> gateway -> upstream AI/sim) nests in Jaeger.
tracing.init_tracing(__import__("os").environ.get("OTEL_SERVICE_NAME", "gateway"))
tracing.instrument_httpx()


from . import enrollment, objectstore


async def _readiness(state: "GatewayState") -> tuple[bool, dict]:
    """Production readiness of the gateway's REQUIRED dependencies.

    Postgres (enrollment/audit store) and MinIO (reference-photo store) must both be
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
    log.info("gateway_starting", port=cfg.port, ulip_live_enabled=cfg.ulip_live_enabled)

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

    # Gate-event capture table + Appendix-C gate KPI views.
    try:
        from .routers import kpi as kpi_router
        await kpi_router.ensure_kpi_gate_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("kpi_gate_schema_boot_failed", error=str(exc))

    # UC-III Final-Completion tables (accidents / transporters / camera-AI /
    # trailer / container / document-OCR / NVR / TRT / bottlenecks / reefer /
    # integration-audit / LDB / RMS-TAS / double-trip). Idempotent, additive —
    # mirrors migration 0024 so a dev DB that never ran it still gets the tables.
    try:
        from . import uc3_ext
        await uc3_ext.ensure_uc3_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("uc3_ext_schema_boot_failed", error=str(exc))

    # CFS-ECY CODECO movements (module 13): the off-dock gate-movement table + dwell
    # view. Idempotent, additive — mirrors migration 0027 so a dev DB that never ran
    # it still gets the objects. Read-only wrt every existing table.
    try:
        from . import cfs_ecy_ext
        await cfs_ecy_ext.ensure_cfs_ecy_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("cfs_ecy_schema_boot_failed", error=str(exc))

    # UC-III gate documents (EIR / PIN / Form-13): mirrors migration 0112 for a dev
    # DB. Additive; touches nothing existing.
    try:
        from . import gate_docs_ext
        await gate_docs_ext.ensure_gate_doc_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("gate_doc_schema_boot_failed", error=str(exc))

    # UC-III lifecycle bus: hand the live WS hub to services/lifecycle_bus so
    # cargo/job/gate/yard/scan milestones fan out to the control room in real time
    # instead of only landing in an event table for polling.
    try:
        from services.lifecycle_bus import set_ws_broadcaster
        set_ws_broadcaster(app.state.gw.ws.broadcast)
    except Exception as exc:  # noqa: BLE001
        log.warning("lifecycle_bus_ws_wire_failed", error=str(exc))

    # Berthing Reports (module 7): per-terminal vessel-call tables + lifecycle events +
    # upload ledger. Idempotent, additive — mirrors migration 0036 so a dev DB that never
    # ran it still gets the objects. Read-only wrt every existing table.
    try:
        from . import berthing_ext
        await berthing_ext.ensure_berthing_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("berthing_schema_boot_failed", error=str(exc))

    # UC-I Marine (vessel-call spine): core.vessel_call + core.vessel_call_event.
    # Idempotent, additive — mirrors migration 0038 so a dev DB that never ran it still
    # gets the objects. Lives in the `core` schema per schema.sql (the agreed source of
    # truth); touches NOTHING in the jnpa schema, so berthing and every other module are
    # unaffected. Read-only wrt every existing table.
    try:
        from . import marine_ext
        await marine_ext.ensure_marine_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("marine_schema_boot_failed", error=str(exc))

    # Performance & Daily Reports (module 12): the perf_* analytical tables for the
    # official JNPA Daily Status Report / monthly TEUs / NLDS-LDB Analytics feeds.
    # Idempotent, additive — mirrors migration 0028 so a dev DB that never ran it
    # still gets the objects. Read-only wrt every existing table.
    try:
        from . import performance_ext
        await performance_ext.ensure_performance_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("performance_schema_boot_failed", error=str(exc))

    # Performance Data Upload (module 12 sub-module): upload lifecycle tables
    # (perf_uploads / perf_import_logs / perf_upload_errors). Idempotent, additive —
    # mirrors migration 0030. Read/write only within this sub-module.
    try:
        from . import performance_upload_ext
        await performance_upload_ext.ensure_performance_upload_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("performance_upload_schema_boot_failed", error=str(exc))

    # Customs module (module 5): the ICEGATE customs-document tables (IGM/OOC/SMTP/
    # RMS/LEO/Shipping Bill) sourced ONLY from official JNPA customer files. Idempotent,
    # additive — mirrors migration 0031 so a dev DB that never ran it still gets the
    # objects. Soft-links to core.cargo BY VALUE (container_no); touches no existing table.
    try:
        from . import customs_ext
        await customs_ext.ensure_customs_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("customs_schema_boot_failed", error=str(exc))

    # Shipping Lines module (module 4: IAL/EAL/EDO) schema — additive; mirrors
    # migration 0032 so a DB that never ran it still gets the objects. Soft-links to
    # core.cargo BY VALUE (container_no); touches no existing table.
    try:
        from . import shipping_lines_ext
        await shipping_lines_ext.ensure_shipping_lines_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("shipping_lines_schema_boot_failed", error=str(exc))

    # Transporters & Drivers Data Upload (UC-III sub-module): the import-ledger tables
    # (td_import_files / td_import_errors) + the masters' import_file_id link.
    # Idempotent, additive — mirrors migration 0035. Upserts into the EXISTING
    # core.transporter / core.driver; creates no business tables.
    try:
        from . import td_upload_ext
        await td_upload_ext.ensure_td_upload_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("td_upload_schema_boot_failed", error=str(exc))

    # Weather module (Open-Meteo Weather + Marine): the core.weather_reading audit /
    # fallback table. Idempotent, additive — mirrors v3 migration 0105 so a dev DB
    # that never ran it still gets the object. Touches no existing table.
    try:
        from . import weather_ext
        await weather_ext.ensure_weather_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("weather_schema_boot_failed", error=str(exc))

    # Traffic module (TomTom Flow + Incidents): the core.traffic_reading audit /
    # fallback table. Idempotent, additive — mirrors v3 migration 0107 so a dev DB
    # that never ran it still gets the object. Touches no existing table.
    try:
        from . import traffic_ext
        await traffic_ext.ensure_traffic_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("traffic_schema_boot_failed", error=str(exc))

    # Air-quality module (OpenAQ): the core.air_quality_readings audit /
    # fallback table. Idempotent, additive — mirrors v3 migration 0108 so a dev DB
    # that never ran it still gets the object. Touches no existing table.
    try:
        from . import air_quality_ext
        await air_quality_ext.ensure_air_quality_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("air_quality_schema_boot_failed", error=str(exc))

    # Logistics module (ULIP): the core.logistics_event / logistics_tracking /
    # ulip_api_audit tables. Idempotent, additive — mirrors v3 migration 0109 so
    # a dev DB that never ran it still gets the objects. Touches no existing table.
    try:
        from . import logistics_ext
        await logistics_ext.ensure_logistics_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("logistics_schema_boot_failed", error=str(exc))

    # JNPA Port-Data API sync: the core.api_sync_state / api_ingest_run /
    # api_record / api_report_snapshot / api_defect_log tables. Idempotent,
    # additive — mirrors v3 migration 0124 so a dev DB that never ran it still
    # gets the objects. Runs regardless of whether the sync loop is enabled
    # (the /api/integrations/jnpa/* reads need the tables).
    try:
        from services.jnpa_sync import ensure_api_ingest_schema
        await ensure_api_ingest_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("jnpa_api_schema_boot_failed", error=str(exc))

    # Rail consumers (rail-fois + rail-form11-icd): the core.rail_import_file /
    # rail_import_error ledger + fois_train_intimation / form11_entry /
    # cto_manifest_entry domain tables. Idempotent, additive — mirrors v3
    # migration 0119 so a dev DB that never ran it still gets the objects.
    try:
        from services.rail.repository import ensure_rail_schema
        await ensure_rail_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("rail_schema_boot_failed", error=str(exc))

    # COARRI/COPRAR consumer (edi-messages group): the core.edi_import_file
    # ledger + edi_vessel_container domain table. Idempotent, additive —
    # mirrors v3 migration 0123 so a dev DB that never ran it still gets
    # the objects.
    try:
        from services.edi_vessel.repository import ensure_edi_vessel_schema
        await ensure_edi_vessel_schema(cfg.postgres_dsn or None)
    except Exception as exc:  # noqa: BLE001
        log.warning("edi_vessel_schema_boot_failed", error=str(exc))

    # Vehicle Master (fleet registry): ensure the table, then migrate the truck-sim
    # fleet into it (idempotent, never clobbering an operator edit) so no existing
    # vehicle disappears when the master is introduced. Best-effort — a sim/DB blip
    # never aborts boot; the /api/vehicles read path re-seeds lazily if empty.
    try:
        from . import fleet
        await fleet.ensure_backend(cfg.postgres_dsn)
        devices: list = []
        url = cfg.truck_api_url.rstrip("/") + "/devices/list"
        try:
            resp = await state.http.get(url, params={"limit": "5000"})
            if resp.status_code == 200:
                devices = list(resp.json().get("devices", []))
        except Exception as exc:  # noqa: BLE001
            log.warning("fleet_seed_sim_unreachable", error=str(exc))
        inserted = await fleet.sync_from_fleet(cfg.postgres_dsn, devices) if devices else 0
        # Reconcile the master with EXISTING driver assignments: every assigned
        # vehicle (drivers.vehicle_no_norm) must exist as a fleet vehicle_id, or the
        # assignment is orphaned (the deployment blocker). Backfills from ALL
        # assignments — not only truck-sim — and NEVER mutates core.driver_identity, so PWA
        # login / JWTs / assignments are untouched.
        backfilled = await fleet.sync_from_assignments(cfg.postgres_dsn)
        log.info("fleet_master_ready", devices_seen=len(devices),
                 inserted=inserted, assignment_backfilled=backfilled)
        # Startup validation: report any ACTIVE driver still without a matching
        # fleet vehicle (should be zero after the backfill).
        orphans = await fleet.orphan_active_drivers(cfg.postgres_dsn)
        if orphans:
            log.error(
                "fleet_orphan_active_drivers",
                count=len(orphans),
                drivers=[{"driver_id": o.get("driver_id"),
                          "vehicle_no_norm": o.get("vehicle_no_norm")} for o in orphans[:50]],
                hint="ACTIVE drivers reference a vehicle absent from core.vehicle",
            )
        else:
            log.info("fleet_assignment_integrity_ok")
    except Exception as exc:  # noqa: BLE001
        log.warning("fleet_master_boot_failed", error=str(exc))

    # Firebase Admin (FCM push transport + Phone-Auth verify) — best-effort init.
    # A missing key/dep just leaves FCM disabled; WebPush + WS carry on unchanged.
    try:
        from . import firebase
        ready = firebase.init_firebase(cfg)
        log.info("firebase_boot", enabled=cfg.firebase_enabled, ready=ready, status=firebase.status())
    except Exception as exc:  # noqa: BLE001
        log.warning("firebase_boot_failed", error=str(exc))

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    # Kafka pumps (blocking consumer threads) — best-effort. The alert pump ALSO
    # mirrors every alert into core.digital_twin_event (+ geofence_events for
    # zone-family kinds); a persistence-only pump lands ANPR reads in
    # core.anpr_read (finally giving that table its writer) + the event timeline.
    alert_pump = KafkaPump(
        state, loop, TOPIC_ALERTS, "alert", "jnpa-gateway-alerts",
        persist=audit.persist_alert_event,
    )
    traffic_pump = KafkaPump(state, loop, TOPIC_TRAFFIC, "traffic", "jnpa-gateway-traffic")
    anpr_pump = KafkaPump(
        state, loop, TOPIC_ANPR, "anpr", "jnpa-gateway-anpr",
        persist=audit.persist_anpr_read, broadcast=False,
    )
    # Cross-twin TAS metering (XT-2): consume UC-II DeferredArrivalWindow
    # events and apply them to the TAS slot book. Also broadcast on WS as
    # type=tas so both frontends see the re-slot live.
    async def _apply_deferred(value) -> None:
        from . import crosstwin
        try:
            win = DeferredArrivalWindow(**value)
        except Exception as exc:  # noqa: BLE001 - reject malformed, keep pump alive
            log.warning("deferred_arrival_invalid", error=str(exc))
            return
        # One applier for both transports: persists to RDS, fans out to the
        # dashboard, and pushes the drivers whose slots moved.
        await crosstwin.apply(app.state.gw, win, transport="KAFKA")

    deferred_pump = KafkaPump(
        state, loop, TOPIC_DEFERRED_ARRIVAL, "tas", "jnpa-gateway-tas",
        persist=_apply_deferred,
    )

    # Start the pumps ONLY when a broker is actually configured — the same guard
    # services/lifecycle_bus.py applies to its producer, for the same reason.
    #
    # Each KafkaPump runs a daemon thread that retries forever, and every retry
    # builds a fresh librdkafka Consumer, which itself spawns several internal
    # threads. In a process with no broker that is pure waste; in the TEST SUITE
    # it is fatal. Each TestClient(app) triggers this startup, so a whole-suite
    # run accumulated four leaking pumps per client across ~80 test files until
    # the process died with "RuntimeError: can't start new thread" — 20 failures
    # and 36 errors that all passed when the same files ran in isolation.
    #
    # The pumps are unconditionally started in every real deployment, because
    # compose and the prod env-file always set KAFKA_BROKERS.
    _pumps = (alert_pump, traffic_pump, anpr_pump, deferred_pump)
    if kafka_configured():
        for _p in _pumps:
            _p.start()
        log.info("kafka_pumps_started", count=len(_pumps))
    else:
        log.info(
            "kafka_pumps_skipped",
            reason="no KAFKA_BROKERS configured",
            detail="set KAFKA_BROKERS to enable the alert/traffic/anpr/tas pumps",
        )

    # Replay persisted cross-twin windows into the in-memory slot book so a
    # restart does not silently drop UC-II's metering (migration 0115).
    try:
        from . import crosstwin
        await crosstwin.restore(app.state.gw)
    except Exception as exc:  # noqa: BLE001 - boot must never depend on this
        log.warning("crosstwin_restore_failed", error=str(exc))

    # MQTT truck-position pump (async task) — best-effort.
    mqtt_task = asyncio.create_task(mqtt_truck_pump(state, stop), name="mqtt-truck-pump")

    # JNPA Port-Data API sync loop (async task) — starts ONLY when a client
    # key is configured AND the scheduler is enabled, so TestClient runs and
    # keyless deployments stay task-free (the Kafka-pump posture above).
    jnpa_task = None
    if cfg.jnpa_portdata_enabled and cfg.jnpa_sync_enabled:
        from services.jnpa_sync import jnpa_sync_loop
        jnpa_task = asyncio.create_task(jnpa_sync_loop(state, stop),
                                        name="jnpa-sync")
        log.info("jnpa_sync_scheduled", interval_s=cfg.jnpa_sync_interval_s)
    else:
        log.info("jnpa_sync_skipped",
                 reason="JNPA_PORTDATA_CLIENT_KEY unset or JNPA_SYNC_ENABLED=false")

    # FASTag toll accumulator (async task) — mandatory for usable toll history:
    # ULIP's FASTAG/01 retains only 72 h per vehicle, so crossings must be swept
    # up continuously or they are gone. Starts ONLY when a ULIP credential is
    # configured (same posture as the sync loop above), so TestClient runs and
    # credential-free deployments stay task-free.
    fastag_task = None
    if getattr(cfg, "ulip_live_enabled", False):
        from services.fastag.poller import fastag_poll_loop
        fastag_task = asyncio.create_task(fastag_poll_loop(state, stop),
                                          name="fastag-poll")
        log.info("fastag_poll_scheduled",
                 interval_s=getattr(cfg, "fastag_poll_interval_s", 3600))
    else:
        log.info("fastag_poll_skipped", reason="ULIP_LIVE_ENABLED is off")

    try:
        yield
    finally:
        stop.set()
        alert_pump.stop()
        traffic_pump.stop()
        anpr_pump.stop()
        deferred_pump.stop()
        mqtt_task.cancel()
        try:
            await mqtt_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if jnpa_task is not None:
            jnpa_task.cancel()
            try:
                await jnpa_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if fastag_task is not None:
            fastag_task.cancel()
            try:
                await fastag_task
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
    # Expose pagination/correlation headers so cross-origin browser clients (the
    # POC-2 Cargo-Twin frontend) can read them — notably X-Total-Count on
    # GET /api/cargo. Additive; unlisted response headers are unaffected.
    expose_headers=["X-Total-Count", "X-Correlation-ID"],
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
    # /api/cargo bodies/paths surface validation failures as 400 (not 422), the
    # same contract as /api/fastag (bad ISO-6346, bad enum, malformed types/JSON).
    if request.url.path.startswith("/api/cargo"):
        return JSONResponse(
            status_code=400,
            content={"error": "validation_error",
                     "detail": jsonable_encoder(exc.errors())},
        )
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
# Console user administration — admin-only (/api/users is scoped to DTCCC_ADMIN
# in auth._POLICY, unlike the public /api/auth bootstrap prefix).
app.include_router(users_router.router)
app.include_router(anpr.router)
app.include_router(vahan.router)
app.include_router(traffic.router)
app.include_router(trucks.router)
app.include_router(ulip.router)
app.include_router(alerts.router)
app.include_router(scenarios.router)
app.include_router(kpi.router)
app.include_router(push.router)
# Notification-pipeline health + delivery-trail introspection (read-only). Sits
# above the push router + the dispatcher; adds GET /api/notifications/health.
app.include_router(notifications_router.router)
app.include_router(geo.router)
app.include_router(reports.router)
# Evidence proxy — streams private-MinIO evidence objects to the browser same-origin
# (so <img>/<video> load without exposing MinIO). Public (no bearer) — see router.
app.include_router(evidence.router)
# Vehicle Violation Detection — orchestration-only enforcement console. Reuses
# ANPR + vehicle_master + driver store + the reports e-Challan schedule + MinIO
# evidence and writes incidents to core.alert (so they appear on the Reports
# page). Mounted after reports because it imports its fine schedule.
app.include_router(violations.router)
# FASTag ULIP surface — /api/fastag/{balance,toll-enroute,transactions}. Thin
# router: auth+validation at the gateway, then client -> mapper -> FastagService
# (the single orchestration point). See gateway/routers/fastag.py.
app.include_router(fastag.router)
# Cargo CRUD — the single shared cargo record on RDS. POC-3 is the common backend
# for both the Traffic Twin (POC-3) and the Cargo Twin (POC-2); POC-2 consumes
# /api/cargo directly and keeps no backend/DB. Thin router → services.cargo
# (CargoService → raw-SQL CargoRepository). See gateway/routers/cargo.py.
# UC-3 what-if simulation — /api/cargo/simulate/* + /api/gate/hourly-profile.
# Registered BEFORE cargo.router so the ordering against GET /api/cargo/{cn} is
# explicit rather than incidental (the simulate paths carry two segments after the
# prefix, so they could not be captured by it either way). READ-ONLY: the layer
# answers "what would this cost" and never writes — see services/cargo/simulation.
app.include_router(cargo_simulation.router)
app.include_router(cargo.router)
app.include_router(scenario_ext.router)
# UC3 Email Processing (/api/email). Reads the admin mailbox over IMAP read-only
# and routes attachments into the EXISTING master tables through the existing
# marine / gate-document upload services. Inert until EMAIL_HOST + EMAIL_USER +
# EMAIL_PASSWORD are set: every route answers "mailbox not configured".
app.include_router(email_processing.router)
# Appendix-C capability services (Empty-Container, Carbon, Gate-Data/Auto-LEO,
# Identity/face-recognition, Parking) — each proxies its upstream and degrades
# to the service's own deterministic logic so the dashboard always renders.
app.include_router(empty_container.router)
app.include_router(carbon.router)
app.include_router(gate_data.router)
app.include_router(journey.router)
app.include_router(meta.router)
app.include_router(workflows.router)
app.include_router(identity.router)
app.include_router(driver_router.router)
app.include_router(drivers_master.router)    # Driver Master & Intelligence (read-only, additive)
app.include_router(vehicle_identity.router)
app.include_router(vehicles.router)
app.include_router(parking.router)
app.include_router(yard.router)             # UC-3 peak-yard truck-arrival management (additive)
app.include_router(debug.router)
app.include_router(control.router)
app.include_router(ai_events.router)
app.include_router(otp.router)
# --- UC-III Final-Completion routers (additive) ---
app.include_router(accidents.router)         # accident lifecycle
app.include_router(transporters.router)      # transporter blacklist + validation
app.include_router(transporters_drivers_upload.router)  # Transporters & Drivers Data Upload (UC-III sub-module)
app.include_router(camera_ai.router)         # camera-AI counting / trailer / container
app.include_router(document_ocr.router)      # document OCR
app.include_router(nvr.router)               # NVR device/stream integration
app.include_router(trt.router)               # ECY TRT KPI
app.include_router(cfs_ecy.router)           # CFS-ECY CODECO gate movements (module 13, read-only)
app.include_router(dq.router)                # Data Quality ledger over core.dq_issue (read-only)
app.include_router(vehicle_registry.router)   # UC3-004 vehicle->transporter registry (MIXED provenance)
app.include_router(corridor_sim.router)      # UC3-005 frozen NH-348 20k simulation (SIMULATED only)
app.include_router(corridor_heatmap.router)  # UC3-020 T-01 corridor congestion heatmap
app.include_router(gate_board.router)        # UC3-021 gate & lane board + UC3-027 CPP metered release
app.include_router(auto_leo.router)          # UC3-040 Auto-LEO four-way join board
app.include_router(trip_search.router)       # UC3-024 trip resolver + UC3-025 visit timeline
app.include_router(customs.router)           # Customs docs (module 5: IGM/OOC/SMTP/RMS/LEO/SB)
app.include_router(gate_documents.router)    # UC-III gate documents (EIR / PIN ticket / Form-13 + TAT)
app.include_router(container_job.router)     # UC-III job spine: assignment + gate/yard/scan events
app.include_router(driver_jobs.router)       # DRIVER-scoped job surface for the mobile PWA
app.include_router(export_lifecycle.router)  # export leg: booking -> Form13 -> VGM -> LEO -> COPRAR -> loaded
app.include_router(securevision.router)       # SecureVision AI video analytics + faces (proxied vendor, /api/sv/*)
app.include_router(shipping_lines.router)     # Shipping Lines (module 4: IAL/EAL/EDO, read-only + import)
app.include_router(berthing.router)          # Berthing Reports (module 7: per-terminal vessel calls + upload)
app.include_router(marine_calls.router)         # UC-I Marine vessel-call spine (module: marine, read-only)
app.include_router(marine_dashboard.router)     # UC-I Marine dashboard boards (5-day plan, UI-028)
app.include_router(marine_live_vessels.router)  # Live AIS vessel positions (MarineTraffic proxy, no DB write)
app.include_router(marine_imports.router)    # UC-I Marine Data-Upload sub-module (CSV: validate/upload/history)
app.include_router(marine_pilotage.router)   # UC-I Marine pilotage movements (read-only; XLSX via marine_imports)
app.include_router(marine_manual_pilot.router)  # UC-I Marine manual pilot assignment (operator fallback; additive paths only)
app.include_router(marine_manual_craft.router)  # UC-I Marine manual craft assignment (operator fallback; additive paths only)
app.include_router(marine_port_craft.router) # UC-I Marine port-craft register (read-only; PDF via marine_imports)
app.include_router(marine_sea_channel.router) # UC-I Marine sea-channel geometry (read-only; SHP zip via marine_imports)
app.include_router(marine_bathymetry.router) # UC-I Marine bathymetry soundings (read-only; PDF/JSON via marine_imports)
app.include_router(marine_vessel.router)     # UC-I Marine vessel master (read-only; VESPRO XML via marine_imports)
app.include_router(marine_state.router)      # UC-I Marine business state (read-only; derived by state_engine)
app.include_router(performance.router)       # Performance & Daily Reports (module 12, read-only, additive)
app.include_router(performance_upload.router)  # Performance Data Upload (module 12 sub-module, admin-only, additive)
app.include_router(bottlenecks.router)       # three-road bottleneck analytics
app.include_router(reefer.router)            # reefer availability
app.include_router(pdp.router)               # PDP adapter
app.include_router(ldb.router)               # LDB adapter
app.include_router(rms_tas.router)           # RMS-TAS persisted appointment surface
app.include_router(weather.router)           # Open-Meteo weather + marine (LIVE→CACHED→SYNTHETIC)
app.include_router(air_quality.router)       # OpenAQ air quality (LIVE→CACHED→DATABASE→SYNTHETIC)
app.include_router(bhuvan.router)            # Bhuvan WMS geospatial layer (ISRO/NRSC, control-plane only)
app.include_router(logistics.router)         # ULIP logistics intelligence (LIVE→CACHED→DATABASE→FALLBACK)
app.include_router(gatishakti.router)        # GatiShakti reference data (toll plazas, road network)
app.include_router(jnpa_api.router)          # JNPA Port-Data API sync (dt.jnpa.in → upload services)
app.include_router(export_chain.router)      # export-lifecycle reads (Form 11, COPRAR, COARRI, synth)
app.include_router(rail.router)              # Rail feeds (FOIS / Form 11 / CTO — read path for the 0119 tables)
app.include_router(edi_vessel.router)        # COARRI/COPRAR vessel-side container moves (read path for 0125)
app.include_router(double_trip.router)       # TT double-trip workflow
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
        "ulip_live_enabled": cfg.ulip_live_enabled,
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
                 "/api/debug/decisions", "/api/ws", "/checkin",
                 "/api/marine/vessels"],
    }


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
