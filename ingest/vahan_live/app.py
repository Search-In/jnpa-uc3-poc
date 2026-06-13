"""FastAPI app: live Surepass-backed Vahan/Sarathi/FASTag adapter.

Exposes the *same* surface as the simulator (`ingest/vahan_sim`) but proxies to
Surepass's commercial KYC API:

    RC      POST https://kyc-api.surepass.io/api/v1/rc/rc-full
    DL      POST https://kyc-api.surepass.io/api/v1/driving-license/driving-license
    FASTag  POST https://kyc-api.surepass.io/api/v1/fastag/fastag-search

Gating: if SUREPASS_API_TOKEN is missing or empty, every lookup returns
HTTP 503 with body {"error": "live_disabled"}. This layer DOES NOT fall back
to the simulator — that decision belongs to the fallback orchestrator
(Prompt 4), which reads jnpa.services to choose sim vs. live.

Successful RC lookups are still written back to jnpa.vehicle_master
(provisional=false), exactly like the simulator, so the dashboard's "verified"
view is identical regardless of which path served the request.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Mapping

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from jnpa_shared.logging import configure_logging, get_logger
from jnpa_shared.schemas import (
    FastagPing,
    SarathiRecord,
    ServiceRegistration,
    VahanRecord,
    is_valid_dl,
    is_valid_plate,
    normalize_plate,
)
from jnpa_shared.vahan_db import ensure_schema, register_service, upsert_vehicle_master

from .config import LiveConfig
from .mappers import map_dl, map_fastag, map_rc

cfg = LiveConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("vahan_live")

def _live_disabled() -> JSONResponse:
    # A fresh response per call (a JSONResponse may only be sent once).
    return JSONResponse(status_code=503, content={"error": "live_disabled"})


def _service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        name=cfg.service_name,
        kind=cfg.service_kind,
        base_url=cfg.base_url,
        healthy=cfg.enabled,
        enabled=cfg.enabled,
        meta={"port": cfg.port, "provider": "surepass"},
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    log.info("vahan_live_starting", enabled=cfg.enabled, base_url=cfg.base_url)
    try:
        await ensure_schema(dsn=cfg.postgres_dsn)
        await register_service(_service_registration(), dsn=cfg.postgres_dsn)
        log.info("service_registered", name=cfg.service_name, kind=cfg.service_kind,
                 enabled=cfg.enabled)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("startup_db_unavailable", error=str(exc))
    yield


app = FastAPI(title="JNPA Vahan/Sarathi/FASTag Live Adapter (Surepass)", version="0.1.0",
              lifespan=_lifespan)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name, "kind": cfg.service_kind,
            "enabled": cfg.enabled}


async def _surepass_post(url: str, id_number: str) -> Mapping[str, Any]:
    """POST {id_number} to a Surepass endpoint with the bearer token.

    Raises HTTPException(502) on upstream/transport errors so the caller sees a
    clean gateway error rather than a stack trace.
    """
    headers = {
        "Authorization": f"Bearer {cfg.surepass_api_token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=cfg.upstream_timeout_s) as client:
            resp = await client.post(url, json={"id_number": id_number}, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("surepass_transport_error", url=url, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "upstream_unreachable"})

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    if resp.status_code >= 400:
        log.warning("surepass_error", url=url, status=resp.status_code, body=resp.text[:300])
        raise HTTPException(status_code=502,
                            detail={"error": "upstream_error", "status": resp.status_code})
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail={"error": "upstream_bad_json"})


@app.get("/vahan/rc/{plate}", response_model=VahanRecord)
async def vahan_rc(plate: str):
    if not cfg.enabled:
        return _live_disabled()
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
    payload = await _surepass_post(cfg.surepass_rc_url, norm)
    rec = map_rc(payload)
    try:
        await upsert_vehicle_master(rec, dsn=cfg.postgres_dsn)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("writeback_failed", plate=norm, error=str(exc))
    return rec


@app.get("/sarathi/dl/{dl_number}", response_model=SarathiRecord)
async def sarathi_dl(dl_number: str):
    if not cfg.enabled:
        return _live_disabled()
    dl = dl_number.strip().upper().replace(" ", "")
    if not is_valid_dl(dl):
        raise HTTPException(status_code=422, detail={"error": "invalid_dl", "dl": dl_number})
    payload = await _surepass_post(cfg.surepass_dl_url, dl)
    return map_dl(payload)


@app.get("/fastag/balance/{plate}", response_model=FastagPing)
async def fastag_balance(plate: str):
    if not cfg.enabled:
        return _live_disabled()
    norm = normalize_plate(plate)
    if not is_valid_plate(norm):
        raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
    payload = await _surepass_post(cfg.surepass_fastag_url, norm)
    return map_fastag(payload, norm)


@app.post("/admin/seed")
async def admin_seed():
    """No-op for the live adapter (its data is upstream). Returns 503 when the
    live path is disabled, mirroring the lookup gating."""
    if not cfg.enabled:
        return _live_disabled()
    return {"reseeded": False, "reason": "live_adapter_has_no_local_dataset"}


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
