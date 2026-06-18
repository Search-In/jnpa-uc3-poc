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

from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from jnpa_shared.backbone import PeriodicPublisher
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import TOPIC_PARKING, ParkingState, ServiceRegistration
from jnpa_shared.vahan_db import ensure_schema, register_service

from . import facilities as fac
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
    _publisher.start()
    try:
        yield
    finally:
        await _publisher.stop()


app = FastAPI(title="JNPA Parking-Availability Service", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name,
            "facilities": len(fac.FACILITIES)}


@app.get("/availability")
async def availability(
    minute_of_day: Optional[int] = Query(
        default=None, ge=0, lt=fac.MINUTES_PER_DAY,
        description="Override the minute of day (0..1439) for deterministic demos.",
    ),
) -> dict:
    """Live per-facility availability board for the dashboard.

    Defaults to the current wall-clock minute; pass ``?minute_of_day=NNN`` for a
    reproducible snapshot. Also refreshes the Prometheus gauges as a side effect.
    """
    minute = _resolve_minute(minute_of_day)
    rows = fac.snapshot(minute)
    _refresh_metrics(rows)
    return {"minute_of_day": minute, "facilities": rows}


@app.get("/facilities")
async def facilities_inventory() -> dict:
    """Static facility inventory (capacity + geo), independent of occupancy."""
    return {"facilities": fac.inventory()}


@app.get("/summary")
async def summary(
    minute_of_day: Optional[int] = Query(
        default=None, ge=0, lt=fac.MINUTES_PER_DAY,
        description="Override the minute of day (0..1439) for deterministic demos.",
    ),
) -> dict:
    """Roll-up totals for the board header (capacity / occupied / available)."""
    minute = _resolve_minute(minute_of_day)
    rows = fac.snapshot(minute)
    _refresh_metrics(rows)
    out = fac.summary(minute)
    out["minute_of_day"] = minute
    return out


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
