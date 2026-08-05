"""FastAPI app: EIR gate-slip OCR ingest.

    POST /infer         multipart image -> structured EIR fields
    POST /infer_batch   multiple images (multipart files[])
    GET  /healthz
    GET  /metrics
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from .config import EirOcrConfig
from .engine import get_engine
from .extract import fields_as_dict
from .metrics import CACHE, INFER, LATENCY, metrics_asgi_app

cfg = EirOcrConfig.from_env()
engine = get_engine(cfg)

app = FastAPI(
    title="JNPA EIR OCR Ingest",
    version="0.1.0",
    description="Tesseract-based EIR / gate-slip OCR with structured field extraction.",
)
app.mount("/metrics", metrics_asgi_app())


def _result_payload(doc_type: str, result) -> Dict[str, Any]:
    return {
        "doc_type": doc_type.upper(),
        "raw_text": result.raw_text,
        "fields": fields_as_dict(result.fields),
        "extras": fields_as_dict(result.extras) if result.extras else {},
        "confidence": result.confidence,
        "source": result.source,
        "timings_ms": result.timings_ms,
        "variants_run": result.variants_run,
        "early_exit": result.early_exit,
        "sha256": result.sha256,
        "engine_ready": result.engine_ready,
        "tesseract_version": result.tesseract_version,
        "error": result.error,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok" if engine.ready else "degraded",
        "service": "eir-ocr",
        "engine": "tesseract",
        "engine_ready": engine.ready,
        "tesseract_version": engine.version,
        "cache_size": cfg.cache_size,
    }


@app.post("/infer")
async def infer(
    file: UploadFile = File(...),
    doc_type: str = Form(default="EIR"),
) -> dict:
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty_file")
    dtype = (doc_type or "EIR").strip().upper() or "EIR"
    t0 = time.perf_counter()
    try:
        result = engine.infer_bytes(raw, doc_type=dtype)
    except Exception as exc:  # noqa: BLE001
        INFER.labels("error").inc()
        raise HTTPException(500, f"ocr_failed:{exc}") from exc
    LATENCY.observe(time.perf_counter() - t0)
    if result.source == "CACHE":
        INFER.labels("cache_hit").inc()
        CACHE.labels("hit").inc()
    else:
        INFER.labels("ok").inc()
        CACHE.labels("miss").inc()
    return _result_payload(dtype, result)


@app.post("/infer_batch")
async def infer_batch(
    files: List[UploadFile] = File(...),
    doc_type: str = Form(default="EIR"),
) -> dict:
    if not files:
        raise HTTPException(400, "no_files")
    dtype = (doc_type or "EIR").strip().upper() or "EIR"
    results: List[Dict[str, Any]] = []
    for f in files:
        raw = await f.read()
        if not raw:
            results.append({"filename": f.filename, "error": "empty_file"})
            continue
        t0 = time.perf_counter()
        try:
            result = engine.infer_bytes(raw, doc_type=dtype)
            LATENCY.observe(time.perf_counter() - t0)
            INFER.labels("ok" if result.source != "CACHE" else "cache_hit").inc()
            payload = _result_payload(dtype, result)
            payload["filename"] = f.filename
            results.append(payload)
        except Exception as exc:  # noqa: BLE001
            INFER.labels("error").inc()
            results.append({"filename": f.filename, "error": str(exc)})
    return {"count": len(results), "results": results}


def run() -> None:
    import uvicorn

    uvicorn.run(
        "eir_ocr.app:app",
        host=cfg.host,
        port=cfg.port,
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":
    run()
