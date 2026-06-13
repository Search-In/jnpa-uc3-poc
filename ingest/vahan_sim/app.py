"""FastAPI app: Vahan (RC) / Sarathi (DL) / FASTag (NETC) schema-faithful simulator.

Exposes the same surface the live Surepass adapter (`ingest/vahan_live`) does,
so the rest of the JNPA UC-III system is API-correct against either:

    GET  /vahan/rc/{plate}        -> VahanRecord  (mirrors Parivahan RC schema)
    GET  /sarathi/dl/{dl_number}  -> SarathiRecord
    GET  /fastag/balance/{plate}  -> FastagPing
    POST /admin/seed              -> reseed the deterministic dataset + fixture
    GET  /healthz
    GET  /metrics                 (Prometheus, mounted)

The dataset is generated deterministically in-memory from `vahan_sim.seed` on
startup. Every successful RC lookup is upserted into `jnpa.vehicle_master`
(provisional=false). The service registers itself in `jnpa.services` on
startup. An artificial 100ms +/- 50ms latency mimics Parivahan's real behaviour.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException

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
from jnpa_shared.vahan_db import (
    ensure_schema,
    register_service,
    upsert_vehicle_master,
)

from .config import SimConfig
from .metrics import LATENCY, LOOKUPS, WRITEBACKS, metrics_asgi_app
from . import seed as seed_mod

cfg = SimConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("vahan_sim")

# In-memory deterministic store, (re)built at startup and on /admin/seed.
_STORE: Dict[str, "seed_mod.SeedRecord"] = {}
_DL_INDEX: Dict[str, SarathiRecord] = {}


def _rebuild_store() -> int:
    """Regenerate the deterministic dataset + DL index. Returns plate count."""
    global _STORE, _DL_INDEX
    _STORE = seed_mod.generate_dataset(cfg.total_plates)
    _DL_INDEX = seed_mod.build_dl_index(_STORE)
    return len(_STORE)


async def _simulate_latency(plate_or_key: str) -> None:
    """Sleep ~mean +/- jitter ms, deterministic per key (so repeats are stable).

    The jitter is derived from a hash of the key rather than wall-clock RNG so
    p95 is reproducible and `Date.now()`-style nondeterminism stays out.
    """
    h = int.from_bytes(hashlib.sha256(plate_or_key.encode()).digest()[:4], "big")
    # Map hash to [-jitter, +jitter].
    span = cfg.latency_jitter_ms
    offset = ((h % 2001) / 1000.0 - 1.0) * span   # -jitter .. +jitter
    delay_ms = max(0.0, cfg.latency_mean_ms + offset)
    await asyncio.sleep(delay_ms / 1000.0)


def _service_registration() -> ServiceRegistration:
    return ServiceRegistration(
        name=cfg.service_name,
        kind=cfg.service_kind,
        base_url=cfg.base_url,
        healthy=True,
        enabled=True,
        meta={"port": cfg.port, "total_plates": cfg.total_plates},
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    n = _rebuild_store()
    log.info("dataset_built", plates=n)

    # Best-effort: register in jnpa.services + ensure schema. DB may not be up
    # yet in some local bring-up orders; don't crash the API if so.
    try:
        await ensure_schema(dsn=cfg.postgres_dsn)
        await register_service(_service_registration(), dsn=cfg.postgres_dsn)
        log.info("service_registered", name=cfg.service_name, kind=cfg.service_kind)
    except Exception as exc:  # pragma: no cover - infra-timing dependent
        log.warning("startup_db_unavailable", error=str(exc))

    # Write the deterministic demo fixture if a writable path is configured.
    try:
        seed_mod.write_fixture(Path(cfg.fixture_path), _STORE, n=cfg.fixture_count)
        log.info("fixture_written", path=cfg.fixture_path, count=cfg.fixture_count)
    except Exception as exc:  # pragma: no cover
        log.warning("fixture_write_failed", error=str(exc))

    yield


app = FastAPI(title="JNPA Vahan/Sarathi/FASTag Simulator", version="0.1.0",
              lifespan=_lifespan)
app.mount("/metrics", metrics_asgi_app())


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "service": cfg.service_name, "kind": cfg.service_kind,
            "plates": len(_STORE)}


@app.get("/vahan/rc/{plate}", response_model=VahanRecord)
async def vahan_rc(plate: str) -> VahanRecord:
    endpoint = "vahan_rc"
    norm = normalize_plate(plate)
    with LATENCY.labels(cfg.service_kind, endpoint).time():
        if not is_valid_plate(norm):
            LOOKUPS.labels(cfg.service_kind, endpoint, "invalid").inc()
            raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
        await _simulate_latency(norm)
        rec = _STORE.get(norm)
        if rec is None:
            LOOKUPS.labels(cfg.service_kind, endpoint, "miss").inc()
            raise HTTPException(status_code=404, detail={"error": "not_found", "plate": norm})

        # Writeback: every successful RC lookup becomes a verified vehicle_master row.
        try:
            await upsert_vehicle_master(rec.rc, dsn=cfg.postgres_dsn)
            WRITEBACKS.labels(cfg.service_kind, "ok").inc()
        except Exception as exc:  # pragma: no cover - infra-timing dependent
            WRITEBACKS.labels(cfg.service_kind, "error").inc()
            log.warning("writeback_failed", plate=norm, error=str(exc))

        LOOKUPS.labels(cfg.service_kind, endpoint, "hit").inc()
        return rec.rc


@app.get("/sarathi/dl/{dl_number}", response_model=SarathiRecord)
async def sarathi_dl(dl_number: str) -> SarathiRecord:
    endpoint = "sarathi_dl"
    dl = dl_number.strip().upper().replace(" ", "")
    with LATENCY.labels(cfg.service_kind, endpoint).time():
        if not is_valid_dl(dl):
            LOOKUPS.labels(cfg.service_kind, endpoint, "invalid").inc()
            raise HTTPException(status_code=422, detail={"error": "invalid_dl", "dl": dl_number})
        await _simulate_latency(dl)
        rec = _DL_INDEX.get(dl)
        if rec is None:
            LOOKUPS.labels(cfg.service_kind, endpoint, "miss").inc()
            raise HTTPException(status_code=404, detail={"error": "not_found", "dl": dl})
        LOOKUPS.labels(cfg.service_kind, endpoint, "hit").inc()
        return rec


@app.get("/fastag/balance/{plate}", response_model=FastagPing)
async def fastag_balance(plate: str) -> FastagPing:
    endpoint = "fastag_balance"
    norm = normalize_plate(plate)
    with LATENCY.labels(cfg.service_kind, endpoint).time():
        if not is_valid_plate(norm):
            LOOKUPS.labels(cfg.service_kind, endpoint, "invalid").inc()
            raise HTTPException(status_code=422, detail={"error": "invalid_plate", "plate": plate})
        await _simulate_latency("fastag:" + norm)
        rec = _STORE.get(norm)
        if rec is None:
            LOOKUPS.labels(cfg.service_kind, endpoint, "miss").inc()
            raise HTTPException(status_code=404, detail={"error": "not_found", "plate": norm})
        LOOKUPS.labels(cfg.service_kind, endpoint, "hit").inc()
        return FastagPing(
            plate=norm,
            tag_id=rec.fastag_tag_id,
            reader_id="netc-lookup",
            bank=rec.fastag_bank,
            balance=rec.fastag_balance,
            status=rec.fastag_status,
        )


@app.post("/admin/seed")
async def admin_seed() -> dict:
    """Rebuild the deterministic dataset + rewrite the demo fixture."""
    n = _rebuild_store()
    fixture: Optional[dict] = None
    with contextlib.suppress(Exception):
        fixture = seed_mod.write_fixture(Path(cfg.fixture_path), _STORE, n=cfg.fixture_count)
    log.info("reseeded", plates=n)
    return {
        "reseeded": True,
        "plates": n,
        "seed": seed_mod.SEED,
        "fixture_count": (fixture or {}).get("count"),
        "fixture_path": cfg.fixture_path,
    }


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
