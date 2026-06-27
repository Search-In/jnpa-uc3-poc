"""FastAPI control plane for the trucking-app telemetry simulator (port 8240).

Owns the ``Fleet`` (20k trucks, scalable to 30k) and the ``Simulator`` that
drives them, and exposes the control surface from the spec:

    GET  /devices?n=20000              current population stats
    POST /devices/scale {target:30000} hot-scale population
    POST /devices/{device_id}/route    override route (TFC-1 gate closure, Prompt 8)
    GET  /devices/{device_id}/route    assigned route polyline (route-deviation, Prompt 7)
    GET  /devices/{device_id}          one device's live snapshot
    GET  /healthz                      liveness
    GET  /metrics                      Prometheus exposition (mounted)

Run with ``truck-sim`` (console script) or ``python -m app`` / ``uvicorn app:app``.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared import tracing

from trucking_app import gates
from trucking_app.config import TruckConfig
from trucking_app.fleet import Fleet
from trucking_app.metrics import metrics_asgi_app
from trucking_app.simulator import Simulator

cfg = TruckConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("trucking_app")

# OpenTelemetry -> Jaeger. The telemetry publisher's Kafka produce injects trace
# context so the anomaly service can continue the trace; a scenario re-route via
# this control plane shows up as a child span of the scenario trace.
import os as _os  # noqa: E402

tracing.init_tracing(_os.environ.get("OTEL_SERVICE_NAME", "truck-sim"))

# Populated in the lifespan; the endpoints read these.
_fleet: Optional[Fleet] = None
_sim: Optional[Simulator] = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _fleet, _sim
    fleet = Fleet(cfg)
    await fleet.start()
    sim = Simulator(cfg, fleet)
    await sim.start()
    _fleet, _sim = fleet, sim
    log.info("control_plane_ready", port=cfg.port, devices=len(fleet.trucks))
    try:
        yield
    finally:
        await sim.stop()
        await fleet.close()
        _fleet, _sim = None, None


app = FastAPI(
    title="JNPA Trucking-App Telemetry Simulator",
    version="0.1.0",
    lifespan=_lifespan,
)
tracing.instrument_fastapi(app)
app.mount("/metrics", metrics_asgi_app())


def _require_fleet() -> Fleet:
    if _fleet is None:
        raise HTTPException(status_code=503, detail={"error": "fleet_not_ready"})
    return _fleet


# ===========================================================================
# Models
# ===========================================================================
class ScaleRequest(BaseModel):
    target: int = Field(ge=0, description="desired device population")


class InjectRequest(BaseModel):
    count: int = Field(ge=1, le=20000, description="number of synthetic trucks")
    tag: str = Field(description="scenario tag, e.g. 'TFC-1:<handle>' (reset key)")
    gate_id: Optional[str] = Field(default=None, description="target gate for all injected trucks")
    state: str = Field(default="EN_ROUTE_TO_PORT", description="initial TruckState")


class RouteOverride(BaseModel):
    # Either an explicit destination or a known gate id to reroute toward.
    gate_id: Optional[str] = Field(default=None, description="reroute to this JNPA gate")
    lat: Optional[float] = None
    lon: Optional[float] = None
    force_state: Optional[str] = Field(
        default=None,
        description="optional state to force (e.g. EN_ROUTE_TO_PORT) before rerouting",
    )


# ===========================================================================
# Endpoints
# ===========================================================================
@app.get("/healthz")
async def healthz() -> dict:
    ready = _fleet is not None and _sim is not None
    return {
        "status": "ok" if ready else "starting",
        "service": "truck-sim",
        "devices": len(_fleet.trucks) if _fleet else 0,
    }


@app.get("/devices")
async def devices(n: int = Query(default=20000, ge=0, description="reference only")) -> dict:
    """Current population stats. ``n`` is accepted (spec: ``?n=20000``) and echoed
    as the reference target, but the live population is what the fleet holds."""
    fleet = _require_fleet()
    stats = fleet.population_stats()
    stats["requested_n"] = n
    return stats


@app.get("/devices/list")
async def devices_list(
    state: Optional[str] = Query(default=None, description="filter to one TruckState"),
    limit: int = Query(default=200, ge=1, le=2000),
) -> dict:
    """A sampled list of live device snapshots for the dashboard.

    The full fleet is 20k+ trucks, far more than a dashboard map needs. This
    returns up to ``limit`` snapshots (optionally filtered to one ``state`` —
    e.g. ``AT_GATE_QUEUE`` for the Driver-Advisory queue) with the same shape as
    ``GET /devices/{id}``. Iteration order over the dict is stable for a given
    population, so the sample is deterministic between calls.
    """
    fleet = _require_fleet()
    want = state.upper() if state else None
    out = []
    for device_id, truck in fleet.trucks.items():
        if want is not None and truck.state.value != want:
            continue
        event = truck.telemetry()
        out.append({
            "device_id": device_id,
            "plate": truck.profile.plate,
            "gate_id": truck.profile.gate_id,
            "state": truck.state.value,
            "position": {"lat": event.lat, "lon": event.lon},
            "speed_kmh": event.speed_kmh,
            "heading": event.heading,
            "remaining_km": round(truck.remaining_km, 3),
            "eta_s": truck.eta_s,
            "segment_id": truck.current_segment_id,
        })
        if len(out) >= limit:
            break
    return {"count": len(out), "filter_state": want, "devices": out}


@app.post("/devices/inject")
async def inject(req: InjectRequest) -> dict:
    """Inject scenario-tagged synthetic trucks (what-if scenarios, Prompt 10).

    Tagged trucks are removable in one call via ``DELETE /devices/tagged/{tag}``,
    which "Reset to baseline" uses to undo the scenario.
    """
    fleet = _require_fleet()
    if req.gate_id is not None and req.gate_id not in gates.GATE_COORDS:
        raise HTTPException(
            status_code=422,
            detail={"error": "unknown_gate", "gate_id": req.gate_id, "known": list(gates.GATE_COORDS)},
        )
    new_ids = fleet.inject_synthetic(
        count=req.count, tag=req.tag, gate_id=req.gate_id, state=req.state
    )
    return {"injected": len(new_ids), "tag": req.tag, "device_ids": new_ids,
            "population": len(fleet.trucks)}


@app.delete("/devices/tagged/{tag}")
async def remove_tagged(tag: str) -> dict:
    """Remove all synthetic trucks for a scenario tag (idempotent)."""
    fleet = _require_fleet()
    removed = fleet.remove_tagged(tag)
    return {"removed": removed, "tag": tag, "population": len(fleet.trucks)}


@app.post("/devices/scale")
async def scale(req: ScaleRequest) -> dict:
    """Hot-scale the population toward ``target`` (bounded by max_devices)."""
    fleet = _require_fleet()
    if req.target > cfg.max_devices:
        raise HTTPException(
            status_code=422,
            detail={"error": "target_exceeds_max", "max_devices": cfg.max_devices},
        )
    population = await fleet.scale_to(req.target)
    return {"scaled": True, "target": req.target, "population": population}


@app.get("/devices/{device_id}")
async def device(device_id: str) -> dict:
    """Live snapshot for one device."""
    fleet = _require_fleet()
    truck = fleet.trucks.get(device_id)
    if truck is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_device", "device_id": device_id})
    event = truck.telemetry()
    return {
        "device_id": device_id,
        "plate": truck.profile.plate,
        "gate_id": truck.profile.gate_id,
        "state": truck.state.value,
        "position": {"lat": event.lat, "lon": event.lon},
        "speed_kmh": event.speed_kmh,
        "heading": event.heading,
        "battery": event.battery,
        "accuracy_m": event.accuracy_m,
        "remaining_km": round(truck.remaining_km, 3),
        "eta_s": truck.eta_s,
        "segment_id": truck.current_segment_id,
    }


@app.get("/devices/{device_id}/route")
async def device_route(device_id: str) -> dict:
    """Return a device's currently-assigned route polyline.

    The behavioural anomaly detector (ai/anomaly, Prompt 7) reads this to compare
    a truck's actual GPS path against its assigned route for the ROUTE_DEVIATION
    rule. ``points`` is an ordered ``[[lat, lon], ...]`` list (empty while the
    fleet has not yet bound a route to a parked/queued truck)."""
    fleet = _require_fleet()
    truck = fleet.trucks.get(device_id)
    if truck is None:
        raise HTTPException(status_code=404, detail={"error": "unknown_device", "device_id": device_id})
    return {
        "device_id": device_id,
        "plate": truck.profile.plate,
        "gate_id": truck.profile.gate_id,
        "state": truck.state.value,
        "points": [[lat, lon] for (lat, lon) in truck.route_points],
        "route_km": round(truck.route_length_km, 3),
        "dist_along_km": round(truck.dist_along_km, 3),
    }


@app.post("/devices/{device_id}/route")
async def override_route(device_id: str, body: RouteOverride) -> dict:
    """Override a device's route — the hook Prompt 8's TFC-1 gate-closure scenario
    uses to reroute trucks away from a closed gate."""
    fleet = _require_fleet()
    if body.gate_id is not None:
        if body.gate_id not in gates.GATE_COORDS:
            raise HTTPException(
                status_code=422,
                detail={"error": "unknown_gate", "gate_id": body.gate_id,
                        "known": list(gates.GATE_COORDS)},
            )
        dest = gates.GATE_COORDS[body.gate_id]
    elif body.lat is not None and body.lon is not None:
        dest = (body.lat, body.lon)
    else:
        raise HTTPException(
            status_code=422,
            detail={"error": "need_gate_id_or_lat_lon"},
        )

    ok = await fleet.override_route(device_id, dest, force_state=body.force_state)
    if not ok:
        raise HTTPException(status_code=404, detail={"error": "unknown_device", "device_id": device_id})
    truck = fleet.trucks[device_id]
    # Bind the new gate to the truck so GET /devices/list (which reports
    # profile.gate_id) and ETA-to-gate reflect the re-route. Without this the
    # route polyline changes but the dashboard's Gate column keeps the old gate.
    if body.gate_id is not None:
        truck.profile.gate_id = body.gate_id
    return {
        "rerouted": True,
        "device_id": device_id,
        "dest": {"lat": dest[0], "lon": dest[1]},
        "state": truck.state.value,
        "route_km": round(truck.route_length_km, 3),
    }


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    if cfg.use_uvloop:
        try:
            import uvloop

            uvloop.install()
            log.info("uvloop_enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("uvloop_unavailable", error=str(exc))

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
