"""/api/carbon — fleet carbon-emissions rollup for the AoI (Appendix C #6).

Proxies the ``carbon`` service (port 8340); on failure, falls back to the
service's own deterministic calculator in-process so the dashboard carbon tile
never blanks. Emission factors are published GHG-Protocol/IPCC-style constants
(see docs/ASSUMPTIONS.md).

    GET  /api/carbon/rollup            -> AoI CO2e total + by-class + moving/idle split
    POST /api/carbon/estimate          -> per-vehicle estimate (compute-only, unchanged)
    POST /api/carbon/calculate         -> per-vehicle estimate PERSISTED to jnpa.carbon_emission
    GET  /api/carbon/history/{vehicle} -> that vehicle's persisted emission ledger
    GET  /api/carbon/history           -> recent emission ledger across vehicles (UI)

The ``/calculate`` + ``/history`` pair (UC-3 audit R6) gives the previously
compute-only calculator a durable ledger. Persistence is DB-backed
(``jnpa.carbon_emission``, migration 0020) and self-provisioned lazily here so it
works on both a fresh init.sql volume and an existing RDS.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Query

from ..logging import get_logger
from ..metrics import REQUESTS, UPSTREAM_LATENCY
from ..state import GatewayState, get_state

log = get_logger("gateway.carbon")

router = APIRouter(prefix="/api/carbon", tags=["carbon"])

# --- durable emission ledger (jnpa.carbon_emission) --------------------------
# Self-provisioning DDL (mirrors the push.py/audit.py pattern) so /calculate and
# /history work even on a volume that predates migration 0020.
_DDL = (
    """CREATE TABLE IF NOT EXISTS jnpa.carbon_emission (
        id                  bigserial PRIMARY KEY,
        vehicle_id          text NOT NULL,
        vehicle_type        text,
        distance_km         numeric,
        fuel_consumed_litre numeric,
        idle_time_minutes   numeric,
        co2_kg              numeric,
        source              text,
        calculation_method  text,
        created_at          timestamptz NOT NULL DEFAULT now())""",
    "CREATE INDEX IF NOT EXISTS idx_carbon_emission_vehicle ON jnpa.carbon_emission (vehicle_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_carbon_emission_created ON jnpa.carbon_emission (created_at DESC)",
)
_READY: Dict[str, bool] = {}


def _dsn_target(dsn: Optional[str]) -> str:
    """host:port/dbname of a DSN for logs, with the password redacted."""
    if not dsn:
        return "(none)"
    try:
        from urllib.parse import urlsplit

        u = urlsplit(dsn)
        host = u.hostname or "?"
        port = f":{u.port}" if u.port else ""
        db = (u.path or "").lstrip("/") or "?"
        return f"{host}{port}/{db}"
    except Exception:  # noqa: BLE001
        return "(unparseable)"


async def _ensure(dsn: Optional[str]) -> None:
    if not dsn or _READY.get(dsn):
        return
    from jnpa_shared.db import execute

    for stmt in _DDL:
        try:
            await execute(stmt, dsn=dsn)
        except Exception as exc:  # noqa: BLE001
            log.debug("carbon_ddl_skipped", error=str(exc))
    _READY[dsn] = True


def _row_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    for k in ("distance_km", "fuel_consumed_litre", "idle_time_minutes", "co2_kg"):
        if d.get(k) is not None:
            d[k] = float(d[k])
    if isinstance(d.get("created_at"), datetime):
        d["created_at"] = d["created_at"].isoformat()
    return d


async def _upstream(state: GatewayState, method: str, path: str,
                    json: Dict[str, Any] | None = None) -> Dict[str, Any] | None:
    url = state.cfg.carbon_url.rstrip("/") + path
    t0 = time.perf_counter()
    try:
        if method == "GET":
            resp = await state.http.get(url)
        else:
            resp = await state.http.post(url, json=json or {})
        UPSTREAM_LATENCY.labels("carbon", "carbon").observe(time.perf_counter() - t0)
        if resp.status_code == 200:
            return resp.json()
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.debug("carbon_upstream_failed", path=path, error=str(exc))
    return None


def _local():
    from carbon import calculator  # type: ignore
    return calculator


@router.get("/rollup")
async def rollup(state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "GET", "/rollup")
    if data is not None:
        REQUESTS.labels("carbon", "ok").inc()
        return {"decision_path": "LIVE", **data}
    calc = _local()
    roll = calc.aoi_rollup(calc.seed_aoi_fleet())
    REQUESTS.labels("carbon", "ok").inc()
    return {"decision_path": "SYNTHETIC", **roll}


@router.post("/estimate")
async def estimate(body: Dict[str, Any] = Body(...),
                   state: GatewayState = Depends(get_state)) -> dict:
    data = await _upstream(state, "POST", "/estimate", body)
    if data is not None:
        REQUESTS.labels("carbon", "ok").inc()
        return {"decision_path": "LIVE", **data}
    calc = _local()
    vc = body.get("vehicle_class", "HGV")
    dist = float(body.get("distance_km", 0))
    payload = float(body.get("payload_tonnes", 0))
    idle = float(body.get("idle_minutes", 0))
    REQUESTS.labels("carbon", "ok").inc()
    return {
        "decision_path": "SYNTHETIC",
        "vehicle_class": vc,
        "moving_kg": calc.trip_emissions_kg(dist, payload, vc),
        "idle_kg": calc.idle_emissions_kg(idle, vc),
        "total_kg": calc.vehicle_emissions_kg(dist, payload, idle, vc),
    }


@router.post("/calculate")
async def calculate(body: Dict[str, Any] = Body(...),
                    state: GatewayState = Depends(get_state)) -> dict:
    """Compute one vehicle's emissions from an activity record AND persist it.

    Body: ``{vehicle_id, distance_km, idle_time_minutes, vehicle_type, payload_tonnes?}``.
    The figure is computed in-process by the same pure calculator the rollup uses
    (published IPCC/DEFRA/GLEC factors), then written to ``jnpa.carbon_emission``.
    Returns ``{emission_id, co2_kg, fuel_consumed_litre, source, ...}``. The
    ``emission_id`` is null when no DB is configured (the figure is still returned).
    """
    calc = _local()
    vehicle_id = str(body.get("vehicle_id") or "").strip()
    if not vehicle_id:
        REQUESTS.labels("carbon", "invalid").inc()
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail={"error": "vehicle_id_required"})

    rec = calc.emission_record(
        vehicle_id=vehicle_id,
        distance_km=float(body.get("distance_km", 0) or 0),
        idle_minutes=float(body.get("idle_time_minutes", 0) or 0),
        vehicle_type=str(body.get("vehicle_type") or "HGV"),
        payload_tonnes=(float(body["payload_tonnes"]) if body.get("payload_tonnes") is not None else None),
    )
    # ``source`` distinguishes a real-telemetry-fed calc from a manual/demo one.
    source = str(body.get("source") or "manual")

    emission_id: Optional[int] = None
    dsn = state.cfg.postgres_dsn
    if dsn:
        await _ensure(dsn)
        # execute_returning() runs the INSERT in a COMMITTED transaction (engine.begin)
        # and hands back the RETURNING row. Using fetch_one() here was the persistence
        # bug: it runs on a non-committing engine.connect(), so the INSERT was rolled
        # back on close — the id came back but the row never landed.
        from jnpa_shared.db import execute_returning

        try:
            inserted = await execute_returning(
                """
                INSERT INTO jnpa.carbon_emission
                    (vehicle_id, vehicle_type, distance_km, fuel_consumed_litre,
                     idle_time_minutes, co2_kg, source, calculation_method)
                VALUES
                    (:vehicle_id, :vehicle_type, :distance_km, :fuel_consumed_litre,
                     :idle_time_minutes, :co2_kg, :source, :calculation_method)
                RETURNING id
                """,
                {
                    "vehicle_id": rec["vehicle_id"],
                    "vehicle_type": rec["vehicle_type"],
                    "distance_km": rec["distance_km"],
                    "fuel_consumed_litre": rec["fuel_consumed_litre"],
                    "idle_time_minutes": rec["idle_time_minutes"],
                    "co2_kg": rec["co2_kg"],
                    "source": source,
                    "calculation_method": rec["calculation_method"],
                },
                dsn=dsn,
            )
            emission_id = int(inserted["id"]) if inserted else None
            if emission_id is None:
                # A committed INSERT ... RETURNING must yield a row — surface loudly.
                log.error("carbon_persist_no_row", vehicle_id=vehicle_id,
                          database=_dsn_target(dsn))
        except Exception as exc:  # noqa: BLE001 — endpoint still returns the figure
            # Do NOT swallow silently: log the vehicle, the error + its class, and the
            # database target so a persistence failure is diagnosable from the logs.
            log.error(
                "carbon_persist_failed",
                vehicle_id=vehicle_id,
                error=str(exc),
                error_type=type(exc).__name__,
                database=_dsn_target(dsn),
            )

    REQUESTS.labels("carbon", "ok").inc()
    return {
        "emission_id": emission_id,
        "vehicle_id": rec["vehicle_id"],
        "vehicle_type": rec["vehicle_type"],
        "distance_km": rec["distance_km"],
        "idle_time_minutes": rec["idle_time_minutes"],
        "fuel_consumed_litre": rec["fuel_consumed_litre"],
        "co2_kg": rec["co2_kg"],
        "moving_kg": rec["moving_kg"],
        "idle_kg": rec["idle_kg"],
        "source": source,
        "calculation_method": rec["calculation_method"],
        "persisted": emission_id is not None,
    }


async def _history(dsn: Optional[str], *, vehicle_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
    if not dsn:
        return []
    await _ensure(dsn)
    from jnpa_shared.db import fetch_all

    try:
        if vehicle_id:
            rows = await fetch_all(
                """SELECT id, vehicle_id, vehicle_type, distance_km, fuel_consumed_litre,
                          idle_time_minutes, co2_kg, source, calculation_method, created_at
                   FROM jnpa.carbon_emission WHERE vehicle_id = :v
                   ORDER BY created_at DESC LIMIT :n""",
                {"v": vehicle_id, "n": limit}, dsn=dsn,
            )
        else:
            rows = await fetch_all(
                """SELECT id, vehicle_id, vehicle_type, distance_km, fuel_consumed_litre,
                          idle_time_minutes, co2_kg, source, calculation_method, created_at
                   FROM jnpa.carbon_emission ORDER BY created_at DESC LIMIT :n""",
                {"n": limit}, dsn=dsn,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("carbon_history_failed", error=str(exc))
        return []
    return [_row_dict(r) for r in rows]


@router.get("/history")
async def history_all(limit: int = Query(default=50, ge=1, le=500),
                      state: GatewayState = Depends(get_state)) -> dict:
    """Recent emission ledger across all vehicles (drives the dashboard ledger)."""
    records = await _history(state.cfg.postgres_dsn, vehicle_id=None, limit=limit)
    REQUESTS.labels("carbon", "ok").inc()
    return {"records": records, "count": len(records)}


@router.get("/history/{vehicle_id}")
async def history_for_vehicle(vehicle_id: str,
                              limit: int = Query(default=50, ge=1, le=500),
                              state: GatewayState = Depends(get_state)) -> dict:
    """One vehicle's persisted emission history (newest first)."""
    records = await _history(state.cfg.postgres_dsn, vehicle_id=vehicle_id.strip(), limit=limit)
    REQUESTS.labels("carbon", "ok").inc()
    return {"vehicle_id": vehicle_id.strip(), "records": records, "count": len(records)}
