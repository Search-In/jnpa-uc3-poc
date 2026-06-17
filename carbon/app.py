"""FastAPI app: carbon-emissions calculator for trailers in the AoI (C6).

Implements Appendix C requirement #6 — carbon-emissions calculation from
fleet-transporter trip activity plus CPP / parking-area dwell, with an
Area-of-Interest (AoI) rollup:

    GET  /healthz   -> liveness {status, service}
    GET  /metrics   -> Prometheus exposition (mounted)
    GET  /rollup    -> AoI rollup over a deterministic synthetic trailer fleet:
                       total CO2e (kg), breakdown by class and by moving/idle
    POST /estimate  -> emissions for one {distance_km, payload_tonnes,
                       idle_minutes, vehicle_class}

Emission factors are published IPCC / GHG-Protocol road-freight factors
(``carbon.factors``); the per-trip / dwell *activity* is simulated, per
``docs/ASSUMPTIONS.md`` ("Carbon (C6)"). Everything is fully deterministic — the
synthetic AoI fleet is SHA-256-seeded in ``carbon.calculator`` with no unseeded
randomness — so the /rollup figure is identical across runs and hosts.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from jnpa_shared.logging import configure_logging, get_logger

from . import calculator
from . import factors
from .config import CarbonConfig
from .metrics import AOI_TOTAL_KG, ESTIMATES, metrics_asgi_app

cfg = CarbonConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("carbon")


def _compute_rollup() -> dict:
    """Build the AoI rollup over the deterministic synthetic trailer fleet."""
    fleet = calculator.seed_aoi_fleet(cfg.aoi_fleet_size)
    rollup = calculator.aoi_rollup(fleet)
    AOI_TOTAL_KG.set(rollup["total_kg"])
    return rollup


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    rollup = _compute_rollup()
    log.info(
        "aoi_rollup_seeded",
        vehicle_count=rollup["vehicle_count"],
        total_kg=rollup["total_kg"],
        seed=calculator.SEED,
    )
    yield


app = FastAPI(title="JNPA Carbon-Emissions Calculator", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


class EstimateRequest(BaseModel):
    """Activity for one vehicle: trip leg + CPP/parking dwell."""

    distance_km: float = Field(0.0, ge=0.0, description="In-AoI trip distance (km).")
    payload_tonnes: float = Field(0.0, ge=0.0, description="Laden payload (tonnes).")
    idle_minutes: float = Field(0.0, ge=0.0, description="CPP/parking dwell (minutes).")
    vehicle_class: str = Field(
        factors.DEFAULT_CLASS, description="HGV | RIGID | LGV | REEFER."
    )


class EstimateResponse(BaseModel):
    vehicle_class: str
    moving_kg: float
    idle_kg: float
    total_kg: float


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name, "kind": cfg.service_kind}


@app.get("/rollup")
async def rollup() -> dict:
    """AoI emissions rollup over the deterministic synthetic trailer fleet."""
    result = _compute_rollup()
    return {
        "area_of_interest": "NH-348 JNPA -> Karal Phata corridor + CPP/parking",
        "seed": calculator.SEED,
        **result,
    }


@app.post("/estimate", response_model=EstimateResponse)
async def estimate(req: EstimateRequest) -> EstimateResponse:
    """Emissions for one vehicle's trip leg + CPP/parking dwell."""
    vclass = (req.vehicle_class or factors.DEFAULT_CLASS).upper()
    moving = calculator.trip_emissions_kg(req.distance_km, req.payload_tonnes, vclass)
    idle = calculator.idle_emissions_kg(req.idle_minutes, vclass)
    total = round(moving + idle, 3)
    ESTIMATES.labels(vehicle_class=vclass).inc()
    return EstimateResponse(
        vehicle_class=vclass, moving_kg=moving, idle_kg=idle, total_kg=total
    )


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
