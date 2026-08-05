"""FastAPI app: empty-container supply-demand optimiser (Appendix C #3).

Builds deterministic supply (ECD + CFS depots with empty-container stock) and
demand (shipping-line / fleet-owner requests, incl. tanker / break-bulk /
cement-bowser cargo variants) books in-memory from ``empty_container.seed`` on
startup, then runs a pure, transparent cost-minimising matcher
(``empty_container.optimizer``) to produce a *probable allocation* across fleet
owners / shipping line / CFS / ECD. The mean estimated turn-round time over
those allocations drives the **TRT-for-empty-from-ECD** KPI.

    GET  /healthz             -> {status, service, depots, demand}
    GET  /metrics             (Prometheus, mounted)
    GET  /allocations         -> {allocations:[...], count}  (runs the optimiser)
    GET  /supply              -> depots + stock
    GET  /demand              -> open demand
    GET  /kpi/trt_empty       -> compute_kpi("trt_empty_ecd", <mean est_trt>).to_dict()
    POST /demand/inject       -> add synthetic demand (scenarios), deterministic id

The books are generated deterministically (seed-hashed, no `Date.now()` / RNG),
so the same bring-up always yields the same allocations and the same KPI — the
demo is reproducible run-to-run and host-to-host. The service registers itself
in ``core.ulip_service`` on startup (best-effort; the API stays up if the DB is not).
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from jnpa_shared.backbone import PeriodicPublisher
from jnpa_shared.kpi import compute_kpi
from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import TOPIC_EMPTY_CONTAINER, EmptyContainerMove

from .config import OptimizerConfig
from .metrics import ALLOCATIONS, OPEN_DEMAND, metrics_asgi_app
from .optimizer import allocate, mean_est_trt
from . import seed as seed_mod
from . import persistence

cfg = OptimizerConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("empty_container")

# In-memory deterministic books, (re)built at startup. Injected demand is kept
# in a separate list so the seeded book stays reproducible.
_SUPPLY: List["seed_mod.Depot"] = []
_DEMAND: List["seed_mod.Demand"] = []
_INJECTED: List["seed_mod.Demand"] = []
_INJECT_SEQ = 0


def _rebuild_books() -> None:
    """Regenerate the deterministic supply + demand books."""
    global _SUPPLY, _DEMAND
    _SUPPLY = seed_mod.supply_book()
    _DEMAND = seed_mod.demand_book(cfg.demand_count)


def _all_demand() -> List["seed_mod.Demand"]:
    """Seeded book plus any scenario-injected demand."""
    return _DEMAND + _INJECTED


# Max moves published per tick (and logged when the snapshot is capped).
_MAX_MOVES = 200


def _moves_snapshot() -> List[EmptyContainerMove]:
    """Current allocations as EmptyContainerMove events for the backbone.

    Runs the optimiser over the current books and maps each :class:`Allocation`
    to one move: ``ecd_id`` is the chosen depot (``supply_depot``) and, since the
    demand/allocation carries no real container number, a stable synthetic one
    (``ECMU{n:07d}``) is derived from the allocation index. Pure function of the
    deterministic books, so a given tick publishes the same moves. Returns ``[]``
    for empty books.
    """
    if not _SUPPLY or not _all_demand():
        return []
    allocs = allocate(_SUPPLY, _all_demand())
    if len(allocs) > _MAX_MOVES:
        log.info("moves_snapshot_capped", total=len(allocs), cap=_MAX_MOVES)
        allocs = allocs[:_MAX_MOVES]
    return [
        EmptyContainerMove(
            container_no=f"ECMU{n:07d}",
            ecd_id=a.supply_depot,
        )
        for n, a in enumerate(allocs)
    ]


# Publishes EmptyContainerMove onto the backbone every few seconds, tagged SIM,
# so the dashboard sees empty-container moves as just another live feed (Phase C).
_publisher = PeriodicPublisher(
    "empty-container", TOPIC_EMPTY_CONTAINER, "jnpa.empty_container.move",
    _moves_snapshot, interval_s=5.0, key_fn=lambda ev: ev.container_no,
    raw_ref_fn=lambda ev: f"container://{ev.container_no}",
)


def _service_registration():
    # Imported lazily so a plain `import empty_container.app` never pulls in the
    # DB stack (keeps the lean test venv importable).
    from jnpa_shared.schemas import ServiceRegistration

    return ServiceRegistration(
        name=cfg.service_name,
        kind=cfg.service_kind,
        base_url=cfg.base_url,
        healthy=True,
        enabled=True,
        meta={"port": cfg.port, "depots": len(_SUPPLY), "demand": len(_all_demand())},
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _rebuild_books()
    OPEN_DEMAND.set(len(_all_demand()))
    log.info("books_built", depots=len(_SUPPLY), demand=len(_DEMAND))

    # Best-effort: register in core.ulip_service + ensure schema. DB may not be up
    # yet in some local bring-up orders; don't crash the API if so.
    try:
        from jnpa_shared.vahan_db import ensure_schema, register_service

        await ensure_schema(dsn=cfg.postgres_dsn)
        await register_service(_service_registration(), dsn=cfg.postgres_dsn)
        log.info("service_registered", name=cfg.service_name, kind=cfg.service_kind)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("startup_db_unavailable", error=str(exc))

    # Materialise the empty-container inventory into RDS (background, idempotent).
    async def _seed() -> None:
        try:
            await persistence.ensure_container_schema(cfg.postgres_dsn)
            await persistence.seed_inventory(_SUPPLY, dsn=cfg.postgres_dsn)
        except Exception as exc:  # noqa: BLE001
            log.warning("container_seed_failed", error=str(exc))

    seed_task = asyncio.create_task(_seed(), name="container-seed")

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


app = FastAPI(title="JNPA Empty-Container Optimiser", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


# Ensure books exist even when the app object is used without the lifespan
# (e.g. some test harnesses import `app` and hit endpoints directly).
if not _SUPPLY or not _DEMAND:
    _rebuild_books()
    OPEN_DEMAND.set(len(_all_demand()))


class InjectDemand(BaseModel):
    """Scenario payload to inject one synthetic demand."""

    cargo_type: str = Field(default="container",
                            description="container | oil_tanker | break_bulk | cement_bowser")
    source: str = Field(default="fleet_owner", description="shipping_line | fleet_owner")
    container_type: Optional[str] = Field(default=None,
                                          description="20GP | 40GP | 40HC | REEFER")
    quantity: int = Field(default=1, ge=1, le=20)
    priority: str = Field(default="normal", description="high | normal | low")
    origin: Optional[str] = Field(default=None, description="known origin label")


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name,
            "depots": len(_SUPPLY), "demand": len(_all_demand())}


@app.get("/supply")
async def supply() -> dict:
    depots = [seed_mod.depot_to_dict(d) for d in _SUPPLY]
    return {"depots": depots, "count": len(depots),
            "total_stock": sum(d.total_stock() for d in _SUPPLY)}


@app.get("/demand")
async def demand() -> dict:
    items = _all_demand()
    return {"demand": [seed_mod.demand_to_dict(d) for d in items], "count": len(items)}


@app.get("/allocations")
async def allocations() -> dict:
    """Run the optimiser over the current books and return probable allocations."""
    allocs = allocate(_SUPPLY, _all_demand())
    # Re-snapshot the per-cargo allocation counters to the current run so the
    # gauge/counter reflect the latest optimisation (idempotent per request).
    for a in allocs:
        ALLOCATIONS.labels(a.cargo_type).inc(0)  # ensure label series exists
    by_cargo: Dict[str, int] = {}
    for a in allocs:
        by_cargo[a.cargo_type] = by_cargo.get(a.cargo_type, 0) + 1
    for cargo_type, n in by_cargo.items():
        ALLOCATIONS.labels(cargo_type).inc(n)
    OPEN_DEMAND.set(len(_all_demand()))
    return {"allocations": [a.to_dict() for a in allocs], "count": len(allocs),
            "unsatisfied": len(_all_demand()) - len(allocs)}


@app.get("/kpi/trt_empty")
async def kpi_trt_empty() -> dict:
    """TRT-for-empty-from-ECD KPI from the mean est_trt over current allocations."""
    allocs = allocate(_SUPPLY, _all_demand())
    value = mean_est_trt(allocs)
    return compute_kpi("trt_empty_ecd", value).to_dict()


@app.post("/demand/inject")
async def demand_inject(payload: InjectDemand) -> dict:
    """Add one synthetic demand (deterministic id keyed off an inject sequence)."""
    global _INJECT_SEQ
    try:
        d = seed_mod.synthetic_demand(
            _INJECT_SEQ,
            cargo_type=payload.cargo_type,
            source=payload.source,
            container_type=payload.container_type,
            quantity=payload.quantity,
            priority=payload.priority,
            origin=payload.origin,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_demand", "msg": str(exc)})
    _INJECTED.append(d)
    _INJECT_SEQ += 1
    OPEN_DEMAND.set(len(_all_demand()))
    log.info("demand_injected", demand_id=d.demand_id, cargo_type=d.cargo_type)
    return {"injected": True, "demand": seed_mod.demand_to_dict(d),
            "open_demand": len(_all_demand())}


# --------------------------------------------------------------------------
# RDS-backed inventory + persisted allocation (Phase 2 · Track 2).
# --------------------------------------------------------------------------
@app.get("/containers/available")
async def containers_available(
    container_type: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict:
    """Available empty containers from RDS inventory (+ per-type summary)."""
    rows = await persistence.available(container_type=container_type, limit=limit, dsn=cfg.postgres_dsn)
    summary = await persistence.available_summary(dsn=cfg.postgres_dsn)
    return {"count": len(rows), "containers": rows, **summary}


@app.post("/containers/allocate")
async def containers_allocate(body: dict = Body(...)) -> dict:
    """Allocate an empty container to a truck/trailer/driver. Persists the
    allocation + movement history + digital_twin_event + decision_audit.

    Body: {container_type, truck_id?, trailer_id?, driver_id?, shipping_line?,
    cargo_type?, allocation_reason?}."""
    container_type = (body.get("container_type") or "").strip()
    if not container_type:
        raise HTTPException(status_code=422, detail={"error": "container_type_required"})
    result = await persistence.allocate_container(
        container_type=container_type, truck_id=body.get("truck_id"),
        trailer_id=body.get("trailer_id"), driver_id=body.get("driver_id"),
        shipping_line=body.get("shipping_line"), cargo_type=body.get("cargo_type"),
        reason=body.get("allocation_reason"), dsn=cfg.postgres_dsn,
    )
    if not result.get("allocated"):
        # 409 when no stock of the requested type — still a normal, audited outcome.
        raise HTTPException(status_code=409, detail=result)
    return result


@app.get("/containers/allocation/history")
async def containers_allocation_history(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    """Persisted empty-container allocation history from RDS."""
    rows = await persistence.allocation_history(limit=limit, dsn=cfg.postgres_dsn)
    return {"count": len(rows), "allocations": rows}


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
