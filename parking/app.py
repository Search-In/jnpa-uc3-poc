"""FastAPI app: real-time parking-availability inside the geo-fenced port.

The parking-availability half of Appendix C requirement #1. Exposes the live
availability board the dashboard renders, plus the static facility inventory and
a roll-up summary for the board header:

    GET  /availability   -> per-facility live availability (snapshot)
    GET  /facilities     -> static facility inventory (capacity + geo)
    GET  /summary        -> roll-up totals for the board header
    GET  /healthz
    GET  /metrics        (Prometheus, mounted)

Occupancy is a deterministic function of (facility_id, minute_of_day) — a smooth
diurnal curve bounded by capacity, with **no** wall-clock RNG — so a given
minute always yields the same board. ``/availability`` and ``/summary`` default
``minute_of_day`` to the current local wall-clock minute (``hour*60+minute``)
for a live service, but accept a ``?minute_of_day=NNN`` override so demos are
fully reproducible. The service registers itself in ``jnpa.services`` on startup.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Query

from jnpa_shared.backbone import PeriodicPublisher
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import TOPIC_PARKING, ParkingState, ServiceRegistration
from jnpa_shared.vahan_db import ensure_schema, register_service

from . import facilities as fac
from . import persistence
from .config import ParkingConfig
from .metrics import PARKING_AVAILABLE, PARKING_FULL_FACILITIES, metrics_asgi_app

cfg = ParkingConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("parking")


def _current_minute_of_day() -> int:
    """Local wall-clock minute of day (hour*60 + minute), 0..1439."""
    now = datetime.now()
    return now.hour * 60 + now.minute


def _resolve_minute(minute_of_day: Optional[int]) -> int:
    """Validate an optional override, falling back to the current minute."""
    if minute_of_day is None:
        return _current_minute_of_day()
    if not (0 <= minute_of_day < fac.MINUTES_PER_DAY):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_minute_of_day", "minute_of_day": minute_of_day},
        )
    return minute_of_day


def _refresh_metrics(rows: list[dict]) -> None:
    """Update the Prometheus gauges from a freshly computed board snapshot."""
    PARKING_AVAILABLE.set(sum(r["available"] for r in rows))
    PARKING_FULL_FACILITIES.set(
        sum(1 for r in rows if r["status"] == fac.STATUS_FULL)
    )


def _parking_state_snapshot() -> list[ParkingState]:
    """Current per-facility availability as ParkingState events for the backbone.

    Pure function of the wall-clock minute (the same deterministic curve the
    HTTP board uses), so a given minute publishes the same states.
    """
    rows = fac.snapshot(_current_minute_of_day())
    return [
        ParkingState(
            facility_id=r["facility_id"],
            capacity=r["capacity"],
            occupied=r["occupied"],
        )
        for r in rows
    ]


# Publishes ParkingState onto the backbone every few seconds, tagged SIM, so the
# dashboard sees parking as just another live feed (Phase C).
_publisher = PeriodicPublisher(
    "parking", TOPIC_PARKING, "jnpa.parking.state", _parking_state_snapshot,
    interval_s=5.0, key_fn=lambda ev: ev.facility_id,
    raw_ref_fn=lambda ev: f"facility://{ev.facility_id}",
)


def _service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        name=cfg.service_name,
        kind=cfg.service_kind,
        base_url=cfg.base_url,
        healthy=True,
        enabled=True,
        meta={"port": cfg.port, "facilities": len(fac.FACILITIES)},
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Best-effort: register in jnpa.services + ensure schema. DB may not be up
    # yet in some local bring-up orders; don't crash the API if so.
    try:
        await ensure_schema(dsn=cfg.postgres_dsn)
        await register_service(_service_registration(), dsn=cfg.postgres_dsn)
        log.info("service_registered", name=cfg.service_name, kind=cfg.service_kind)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("startup_db_unavailable", error=str(exc))

    # Seed the real inventory (facilities + slots) into RDS — background so the
    # API is READY immediately; idempotent so a restart never duplicates.
    async def _seed() -> None:
        try:
            await persistence.ensure_parking_schema(cfg.postgres_dsn)
            await persistence.seed_inventory(fac.FACILITIES, dsn=cfg.postgres_dsn)
        except Exception as exc:  # noqa: BLE001
            log.warning("parking_seed_failed", error=str(exc))

    seed_task = asyncio.create_task(_seed(), name="parking-seed")

    _publisher.start()
    try:
        yield
    finally:
        seed_task.cancel()
        try:
            await seed_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await _publisher.stop()


app = FastAPI(title="JNPA Parking-Availability Service", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name,
            "facilities": len(fac.FACILITIES)}


def _board_row(r: dict) -> dict:
    """Shape an RDS availability row for the dashboard board (adds name/lat/lon)."""
    loc = r.get("location") or {}
    return {
        "facility_id": r["facility_id"],
        "name": r.get("facility_name"),
        "lat": loc.get("lat"),
        "lon": loc.get("lon"),
        "gate_id": loc.get("gate_id"),
        "capacity": int(r.get("capacity") or 0),
        "occupied": int(r.get("occupied") or 0),
        "available": int(r.get("available") or 0),
        "free_pct": r.get("free_pct"),
        "status": r.get("status"),
    }


@app.get("/availability")
async def availability() -> dict:
    """Live per-facility availability — computed from REAL slot state in RDS
    (no sine curve, no synthetic occupancy). If the DB is unreachable / not yet
    seeded, returns source="unavailable" so the dashboard shows an explicit error
    state rather than fabricated numbers."""
    rows = await persistence.availability(dsn=cfg.postgres_dsn)
    if rows:
        board = [_board_row(r) for r in rows]
        _refresh_metrics(board)
        return {"source": "rds", "facilities": board}
    return {"source": "unavailable", "facilities": []}


@app.get("/facilities")
async def facilities_inventory() -> dict:
    """Facility inventory (capacity + geo) from RDS (fallback to static seed)."""
    rows = await persistence.facilities_inventory(dsn=cfg.postgres_dsn)
    if rows:
        return {"source": "rds", "facilities": rows}
    return {"source": "fallback", "facilities": fac.inventory()}


@app.get("/summary")
async def summary() -> dict:
    """Roll-up totals for the board header (capacity / occupied / available), RDS-backed."""
    rows = await persistence.availability(dsn=cfg.postgres_dsn)
    if rows:
        board = [_board_row(r) for r in rows]
        _refresh_metrics(board)
        return {"source": "rds", **await persistence.summary(dsn=cfg.postgres_dsn)}
    return {"source": "unavailable", "capacity": 0, "occupied": 0,
            "available": 0, "facilities": 0, "full": 0}


# --- allocation / release / history / violations (RDS-backed) --------------
@app.post("/allocate")
async def allocate(body: dict = Body(...)) -> dict:
    """Allocate a free slot to a vehicle. Body: {facility_id, vehicle_id, driver_id?}."""
    facility_id = (body.get("facility_id") or "").strip()
    vehicle_id = (body.get("vehicle_id") or "").strip()
    if not facility_id or not vehicle_id:
        raise HTTPException(status_code=422, detail={"error": "facility_id_and_vehicle_id_required"})
    return await persistence.allocate(
        facility_id=facility_id, vehicle_id=vehicle_id,
        driver_id=body.get("driver_id"), dsn=cfg.postgres_dsn,
    )


@app.post("/release")
async def release(body: dict = Body(...)) -> dict:
    """Release a vehicle's active parking slot. Body: {vehicle_id}."""
    vehicle_id = (body.get("vehicle_id") or "").strip()
    if not vehicle_id:
        raise HTTPException(status_code=422, detail={"error": "vehicle_id_required"})
    return await persistence.release(vehicle_id=vehicle_id, dsn=cfg.postgres_dsn)


@app.post("/violation")
async def violation(body: dict = Body(...)) -> dict:
    """Record an ILLEGAL_PARKING / NO_PARKING_VIOLATION event."""
    return await persistence.raise_violation(
        event_type=body.get("event_type", "ILLEGAL_PARKING"),
        vehicle_id=(body.get("vehicle_id") or "").strip() or "UNKNOWN",
        facility_id=body.get("facility_id"),
        detail=body.get("detail") or {}, dsn=cfg.postgres_dsn,
    )


@app.get("/history")
async def history(vehicle_id: Optional[str] = Query(default=None),
                  limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    """Entry/exit transaction history from RDS."""
    rows = await persistence.history(vehicle_id=vehicle_id, limit=limit, dsn=cfg.postgres_dsn)
    return {"count": len(rows), "transactions": rows}


@app.get("/violations")
async def list_violations(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    """Parking violation / overflow events from RDS."""
    rows = await persistence.violations(limit=limit, dsn=cfg.postgres_dsn)
    return {"count": len(rows), "violations": rows}


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
