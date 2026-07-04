"""/api/evidence — stream stored evidence objects from MinIO to the browser.

Evidence (violation frames, ANPR crops) lives in the PRIVATE MinIO `evidence`
bucket, reachable only inside the docker network (``minio:9000``). The dashboard's
``<img>``/``<video>`` tags can't reach that host, so this route proxies the object
through the gateway — same origin as the app — while MinIO stays private (no port
exposed, no public bucket policy).

Public (no bearer) because an ``<img>`` element can't send an Authorization
header; only GETs of objects already stored under the evidence bucket are served,
and path traversal is rejected. For production, swap this for time-limited
pre-signed URLs; for the PoC a same-origin proxy is the clean, safe default.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from ..logging import get_logger
from ..metrics import REQUESTS

log = get_logger("gateway.evidence")

router = APIRouter(prefix="/api/evidence", tags=["evidence"])

_CONTENT_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
}


def _bucket() -> str:
    return os.environ.get("ANOMALY_EVIDENCE_BUCKET", "evidence").strip()


def _minio():
    from minio import Minio  # lazy import — optional dependency

    return Minio(
        os.environ.get("MINIO_ENDPOINT", "minio:9000").strip(),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "").strip(),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "").strip(),
        secure=os.environ.get("MINIO_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def _fetch(object_path: str) -> bytes:
    """Blocking MinIO read (run off the event loop via run_in_threadpool)."""
    resp = None
    try:
        client = _minio()
        resp = client.get_object(_bucket(), object_path)
        return resp.read()
    finally:
        try:
            if resp is not None:
                resp.close()
                resp.release_conn()
        except Exception:  # noqa: BLE001
            pass


@router.get("/{object_path:path}")
async def get_evidence(object_path: str) -> Response:
    """Stream one evidence object from the private MinIO bucket to the browser."""
    # Defensive: reject empty paths and traversal — only serve stored objects.
    if not object_path or ".." in object_path or object_path.startswith("/"):
        raise HTTPException(status_code=404, detail={"error": "not_found"})
    try:
        data = await run_in_threadpool(_fetch, object_path)
    except Exception as exc:  # noqa: BLE001
        log.info("evidence_fetch_failed", object=object_path, error=str(exc))
        REQUESTS.labels("evidence", "not_found").inc()
        raise HTTPException(status_code=404, detail={"error": "evidence_not_found"})
    ext = os.path.splitext(object_path)[1].lower()
    ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
    REQUESTS.labels("evidence", "ok").inc()
    return Response(
        content=data,
        media_type=ctype,
        headers={"Cache-Control": "public, max-age=3600"},
    )
