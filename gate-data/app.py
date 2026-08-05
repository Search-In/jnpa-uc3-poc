"""FastAPI app: gate-data capture + Auto-LEO (Let Export Order) reconciliation.

Implements Appendix C requirements #4 and #5 for the JNPA UC-III PoC: capture of
e-seal, Form 13, weighbridge and ICEGATE data per export container/vehicle pair,
container/vehicle identity matching, and the Customs alerts & flags that gate an
automated Let Export Order.

    GET  /healthz                  -> liveness + container count
    GET  /metrics                  (Prometheus, mounted)
    GET  /records/{container_no}   -> the four raw captured source records
    POST /leo  {container_no}      -> reconcile() one container -> AutoLeoResult
    GET  /leo/queue                -> reconcile every container (Auto-LEO panel)
    GET  /customs/flags            -> all current Customs flags (Customs feed)

The dataset is generated deterministically in-memory from ``gate_data.seed`` on
startup. The service registers itself in ``core.ulip_service`` on startup (best
effort; the API still serves if the DB is not up yet).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from jnpa_shared.backbone import PeriodicPublisher
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import TOPIC_WEIGHBRIDGE, ServiceRegistration, WeighbridgeReading
from jnpa_shared.vahan_db import ensure_schema, register_service

from .config import GateConfig
from .leo import customs_alerts, reconcile, reconcile_all
from .leo import _FLAG_SEVERITY as FLAG_SEVERITY
from .metrics import CUSTOMS_FLAGS, RECONCILIATIONS, metrics_asgi_app
from . import seed as seed_mod
from . import icegate_sim
from . import persistence
from . import providers
from . import customs_adapter

cfg = GateConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("gate_data")

# In-memory deterministic store, (re)built at startup.
_STORE: Dict[str, "seed_mod.GateRecord"] = {}


def _rebuild_store() -> int:
    """Regenerate the deterministic dataset. Returns the container count."""
    global _STORE
    _STORE = seed_mod.generate_dataset(cfg.total_containers)
    return len(_STORE)


def _service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        name=cfg.service_name,
        kind=cfg.service_kind,
        base_url=cfg.base_url,
        healthy=True,
        enabled=True,
        meta={"port": cfg.port, "total_containers": cfg.total_containers},
    )


def _record_to_dict(rec: "seed_mod.GateRecord") -> dict:
    """Shape one container's four captured records for the /records response."""
    from dataclasses import asdict

    return {
        "container_no": rec.container_no,
        "vehicle_plate": rec.vehicle_plate,
        "eseal": asdict(rec.eseal),
        "form13": asdict(rec.form13),
        "weighbridge": asdict(rec.weighbridge),
        "icegate": icegate_sim.icegate_message(rec.icegate),
    }


# Cap the per-tick weighbridge snapshot so a large dataset never floods the
# backbone in one go (logged when it bites).
_WEIGHBRIDGE_SNAPSHOT_CAP = 200


def _weighbridge_snapshot() -> list[WeighbridgeReading]:
    """Current weighbridge readings as backbone events.

    Maps each seeded GateRecord's captured weighbridge reading into the
    canonical WeighbridgeReading. The dataset is built into ``_STORE`` at
    startup; before that (or if empty) we publish nothing. There is no explicit
    weighbridge id in the seed corpus, so we derive a single stable lane id.
    """
    if not _STORE:
        return []
    events: list[WeighbridgeReading] = []
    for rec in _STORE.values():
        wb = rec.weighbridge
        events.append(
            WeighbridgeReading(
                wb_id="WB-1",
                vehicle_no=wb.vehicle_plate,
                gross_wt_kg=float(wb.measured_wt_kg),
            )
        )
        if len(events) >= _WEIGHBRIDGE_SNAPSHOT_CAP:
            log.info("weighbridge_snapshot_capped", cap=_WEIGHBRIDGE_SNAPSHOT_CAP,
                     total=len(_STORE))
            break
    return events


# Publishes WeighbridgeReading onto the backbone every few seconds, tagged SIM,
# so the dashboard sees gate-data as just another live feed (Phase C).
_publisher = PeriodicPublisher(
    "gate-data", TOPIC_WEIGHBRIDGE, "jnpa.weighbridge.read", _weighbridge_snapshot,
    interval_s=5.0, key_fn=lambda ev: ev.vehicle_no,
    raw_ref_fn=lambda ev: f"weighbridge://{ev.wb_id}#veh={ev.vehicle_no}",
)


async def _persist_reconciliation(result, *, source_mode: str = "sim") -> None:
    """Persist one Auto-LEO outcome + raise durable customs alerts for its flags."""
    await persistence.record_reconciliation(
        container_no=result.container_no,
        vehicle_plate=result.vehicle_plate,
        leo_ready=result.leo_ready,
        customs_flags=result.customs_flags,
        checks=result.checks,
        source_mode=source_mode,
        dsn=cfg.postgres_dsn,
    )
    for flag in result.customs_flags:
        await persistence.raise_customs_alert(
            flag=flag,
            severity=FLAG_SEVERITY.get(flag, "warning"),
            container_no=result.container_no,
            vehicle_plate=result.vehicle_plate,
            payload={"checks": {k: result.checks.get(k) for k in (
                "weight_discrepancy_pct", "icegate_leo_status", "id_match") if k in result.checks}},
            dsn=cfg.postgres_dsn,
        )


async def _persist_dataset_once() -> None:
    """One-shot: land every captured source record + reconciliation in RDS.

    Runs in the background at boot so the service is READY immediately. Idempotent
    (captures upsert on container+type+captured_at; customs alerts dedup by
    container+flag) so a restart does not duplicate history. This is what makes
    the gate/customs domain RDS-backed from the first boot.
    """
    if not cfg.postgres_dsn:
        log.warning("gate_persist_skipped_no_dsn")
        return
    await persistence.ensure_gate_schema(cfg.postgres_dsn)
    # When the ICEGATE customs adapter is on, ICEGATE captures come from the REAL
    # customs tables (below), not the seed — so skip the synthetic ICEGATE upsert.
    # e-Seal / Form-13 / Weighbridge are unaffected (still seeded / SIM).
    adapter_on = customs_adapter.enabled()
    if adapter_on:
        log.info("icegate_adapter_enabled")
    else:
        # Symmetric rollback: the adapter is OFF, so purge any LIVE ICEGATE rows left
        # by a prior adapter run before re-seeding the synthetic ICEGATE below — no
        # stale LIVE records may remain after rollback.
        log.info("icegate_adapter_disabled")
        removed = await customs_adapter.purge_live_icegate(cfg.postgres_dsn)
        if removed:
            log.info("icegate_rollback_executed", live_rows_removed=removed)
    captured = 0
    for cn, rec in _STORE.items():
        for source in providers.SOURCES:
            if source == "ICEGATE" and adapter_on:
                continue
            payload, status, captured_at, mode_used = await providers.capture_source(
                source, cn, rec, dsn=cfg.postgres_dsn
            )
            await persistence.upsert_capture(
                capture_type=source, container_no=cn,
                vehicle_plate=rec.vehicle_plate, gate_id=None,
                source_mode=mode_used, status=status,
                captured_at=captured_at, payload=payload, dsn=cfg.postgres_dsn,
            )
            captured += 1
    if adapter_on:
        synced = await customs_adapter.sync_icegate_captures(cfg.postgres_dsn)
        log.info("icegate_adapter_active", icegate_captures=synced)
    results = reconcile_all(dataset=_STORE, weight_tolerance_pct=cfg.weight_tolerance_pct)
    flagged = 0
    for result in results:
        await _persist_reconciliation(result, source_mode="sim")
        flagged += 1 if result.customs_flags else 0
    log.info("gate_dataset_persisted", captures=captured, reconciliations=len(results),
             flagged=flagged)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    n = _rebuild_store()
    log.info("dataset_built", containers=n)

    # Best-effort registration in core.ulip_service + ensure schema. DB may not be
    # up yet in some local bring-up orders; don't crash the API if so.
    try:
        await ensure_schema(dsn=cfg.postgres_dsn)
        await register_service(_service_registration(), dsn=cfg.postgres_dsn)
        log.info("service_registered", name=cfg.service_name, kind=cfg.service_kind)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("startup_db_unavailable", error=str(exc))

    # Land the full capture inventory + reconciliations in RDS (background so the
    # API is READY immediately; best-effort so a DB blip never aborts boot).
    persist_task = asyncio.create_task(_persist_dataset_once(), name="gate-persist")

    _publisher.start()
    try:
        yield
    finally:
        persist_task.cancel()
        try:
            await persist_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _publisher.stop()


app = FastAPI(title="JNPA Gate-Data / Auto-LEO Simulator", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name, "kind": cfg.service_kind,
            "containers": len(_STORE), "providers": providers.providers_status()}


@app.get("/providers")
async def provider_status() -> dict:
    """Per-source SIM|LIVE mode + configured flag (e-Seal/Form-13/Weighbridge/ICEGATE)."""
    return {"sources": providers.providers_status()}


@app.get("/records/{container_no}")
async def records(container_no: str) -> dict:
    """Return the four raw captured source records for a container."""
    rec = _STORE.get(container_no)
    if rec is None:
        raise HTTPException(status_code=404,
                            detail={"error": "not_found", "container_no": container_no})
    return _record_to_dict(rec)


class LeoRequest(BaseModel):
    container_no: str


@app.post("/leo")
async def leo(body: LeoRequest) -> dict:
    """Reconcile one container's gate data into an Auto-LEO result."""
    if container_no := body.container_no.strip():
        if container_no not in _STORE:
            raise HTTPException(status_code=404,
                                detail={"error": "not_found", "container_no": container_no})
        result = reconcile(container_no, dataset=_STORE, weight_tolerance_pct=cfg.weight_tolerance_pct)
        RECONCILIATIONS.labels("ready" if result.leo_ready else "blocked").inc()
        for flag in result.customs_flags:
            CUSTOMS_FLAGS.labels(flag).inc()
        # Persist this reconciliation + any customs flags (durable audit trail).
        await _persist_reconciliation(result, source_mode="sim")
        return result.to_dict()
    raise HTTPException(status_code=422, detail={"error": "missing_container_no"})


@app.get("/leo/queue")
async def leo_queue() -> dict:
    """Reconcile every seeded container — the Auto-LEO panel feed."""
    results = reconcile_all(dataset=_STORE, weight_tolerance_pct=cfg.weight_tolerance_pct)
    ready = sum(1 for r in results if r.leo_ready)
    return {
        "total": len(results),
        "ready": ready,
        "blocked": len(results) - ready,
        "results": [r.to_dict() for r in results],
    }


@app.get("/customs/flags")
async def customs_flags() -> dict:
    """All current Customs flags across containers — the Customs feed."""
    results = reconcile_all(dataset=_STORE, weight_tolerance_pct=cfg.weight_tolerance_pct)
    alerts: list[dict] = []
    by_flag: Dict[str, int] = {}
    for result in results:
        for flag in result.customs_flags:
            by_flag[flag] = by_flag.get(flag, 0) + 1
        alerts.extend(customs_alerts(result))
    return {
        "total": len(alerts),
        "by_flag": by_flag,
        "alerts": alerts,
    }


# --------------------------------------------------------------------------
# RDS-backed history (the dashboards read these — single source of truth).
# --------------------------------------------------------------------------
@app.get("/captures")
async def captures(
    type: Optional[str] = Query(default=None),
    container_no: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Persisted gate captures (e-Seal/Form-13/Weighbridge/ICEGATE) from RDS."""
    rows = await persistence.recent_captures(
        capture_type=type, container_no=container_no, limit=limit, dsn=cfg.postgres_dsn
    )
    return {"count": len(rows), "captures": rows}


@app.get("/reconciliations")
async def reconciliations(
    ready: Optional[bool] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict:
    """Persisted Auto-LEO reconciliation outcomes from RDS."""
    rows = await persistence.recent_reconciliations(
        leo_ready=ready, limit=limit, dsn=cfg.postgres_dsn
    )
    return {"count": len(rows), "reconciliations": rows}


@app.get("/customs/history")
async def customs_history(limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """Durable customs feed from core.alert (kind=CUSTOMS_FLAG), survives restart."""
    rows = await persistence.customs_flag_history(limit=limit, dsn=cfg.postgres_dsn)
    return {"count": len(rows), "alerts": rows}


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
