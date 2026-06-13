"""FastAPI app for the ANPR + OCR inference service (port 8301).

Endpoints:
    POST /infer        multipart image      -> {plate, conf, bbox, ...}
    POST /infer_batch  JSON {images:[b64]}  -> {results:[...]}
    GET  /eval         run the held-out benchmark -> metrics + OCR_TARGET_MET
    GET  /healthz      readiness (model/weights status)
    GET  /metrics      Prometheus

Consumed by ``ingest/anpr`` which POSTs plate crops to ``/infer`` when
DRY_RUN=false (see that service's emitter).
"""
from __future__ import annotations

import base64
import binascii
from contextlib import asynccontextmanager
from typing import List, Optional

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, UploadFile
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic import BaseModel

from jnpa_shared.logging import configure_logging, get_logger

from .config import AnprAiConfig
from .evaluator import run_eval
from .pipeline import AnprPipeline
from . import storage

cfg = AnprAiConfig.from_env()
configure_logging(cfg.log_level)
log = get_logger("anpr.app")

# --- Prometheus -------------------------------------------------------------
INFER_REQUESTS = Counter("anpr_infer_requests_total", "Total /infer(+batch) images", ["endpoint"])
INFER_VALID = Counter("anpr_infer_valid_total", "Plates that validated against the grammar")
INFER_LATENCY = Histogram("anpr_infer_seconds", "Per-image inference latency", ["endpoint"])

_pipeline: Optional[AnprPipeline] = None


def get_pipeline() -> AnprPipeline:
    assert _pipeline is not None, "pipeline not initialised"
    return _pipeline


def _decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=422, detail={"error": "bad_image"})
    return img


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _pipeline
    # Reconcile weights with MinIO (best-effort) before loading the model.
    synced = storage.sync_weights(cfg)
    if synced:
        cfg.yolo_weights = synced
    _pipeline = AnprPipeline(cfg)
    readiness = _pipeline.warm()
    log.info("anpr_ai_ready", port=cfg.port, **readiness)
    yield


app = FastAPI(title="JNPA ANPR + OCR Inference", version="0.1.0", lifespan=_lifespan)
app.mount("/metrics", make_asgi_app())


# --- request/response models -----------------------------------------------
class InferBatchRequest(BaseModel):
    images: List[str]  # base64-encoded image bytes (jpeg/png)


@app.get("/healthz")
async def healthz() -> dict:
    p = get_pipeline()
    return {
        "status": "ok",
        "service": "anpr",
        "weights_sha256": p.detector.weights_sha256,
        "degraded": p.detector._ml_ok is not True or p.ocr._ml_ok is not True,
    }


@app.post("/infer")
async def infer(image: UploadFile = File(...)) -> dict:
    """Multipart image -> recognised plate. Accepts a full frame OR a plate crop;
    the detector finds the ROI either way."""
    INFER_REQUESTS.labels(endpoint="infer").inc()
    data = await image.read()
    img = _decode_image(data)
    with INFER_LATENCY.labels(endpoint="infer").time():
        res = get_pipeline().infer(img)
    if res.valid:
        INFER_VALID.inc()
    return res.as_dict()


@app.post("/infer_batch")
async def infer_batch(req: InferBatchRequest) -> dict:
    """JSON list of base64 images -> list of results (order preserved)."""
    pipeline = get_pipeline()
    results: List[dict] = []
    for b64 in req.images:
        INFER_REQUESTS.labels(endpoint="infer_batch").inc()
        try:
            raw = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            results.append({"error": "bad_base64"})
            continue
        try:
            img = _decode_image(raw)
        except HTTPException:
            results.append({"error": "bad_image"})
            continue
        with INFER_LATENCY.labels(endpoint="infer_batch").time():
            res = pipeline.infer(img)
        if res.valid:
            INFER_VALID.inc()
        results.append(res.as_dict())
    return {"count": len(results), "results": results}


@app.get("/eval")
async def eval_endpoint(n: Optional[int] = None) -> dict:
    """Run the held-out benchmark (clean / dust+haze / night) and return metrics
    including the OCR_TARGET_MET gate."""
    import asyncio

    metrics = await asyncio.to_thread(run_eval, get_pipeline(), cfg, n)
    log.info("eval_done", **{k: metrics[k] for k in
                             ("combined_weighted_accuracy_pct", "OCR_TARGET_MET")})
    return metrics


def run() -> None:  # pragma: no cover - container entrypoint
    import uvicorn

    uvicorn.run(app, host=cfg.host, port=cfg.port, log_config=None)


if __name__ == "__main__":  # pragma: no cover
    run()
