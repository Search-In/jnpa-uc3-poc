"""MinIO persistence for driver-enrolment reference photos (Identity / C2).

A driver's approved reference frame is stored in the ``driver-enrolment`` bucket
as ``{driver_id}.jpg`` and an object URL is returned for the enrolment record.

All operations are best-effort and degrade gracefully: if the ``minio`` client is
absent or MinIO is unreachable, ``put_reference_photo`` returns ``None`` and the
caller keeps the base64 frame in Postgres instead — the enrolment still completes.
Mirrors ai/anomaly/storage.py's approach (lazy import, never crash the request).
"""
from __future__ import annotations

import io
import os
from typing import Optional

from .logging import get_logger

log = get_logger("gateway.objectstore")


def _endpoint() -> str:
    return os.environ.get("MINIO_ENDPOINT", "minio:9000").strip()


def _bucket() -> str:
    return os.environ.get("DRIVER_ENROL_BUCKET", "drivers").strip()


def _public_base() -> str:
    # Host-reachable base for the stored object URL (S3 port). Mirrors the
    # ANOMALY_EVIDENCE_URL_BASE convention. Falls back to the in-network endpoint.
    base = os.environ.get("DRIVER_ENROL_URL_BASE", "").strip()
    if base:
        return base.rstrip("/")
    return f"http://{_endpoint()}/{_bucket()}"


def enabled() -> bool:
    """True when MinIO credentials are configured (object storage is in play)."""
    return bool(
        os.environ.get("MINIO_ACCESS_KEY", "").strip()
        and os.environ.get("MINIO_SECRET_KEY", "").strip()
    )


def _client():
    from minio import Minio  # lazy import — optional dependency

    return Minio(
        _endpoint(),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "").strip(),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "").strip(),
        secure=os.environ.get("MINIO_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"},
    )


def _ensure_bucket(client, bucket: str) -> bool:
    try:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            log.info("minio_bucket_created", bucket=bucket)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_unavailable", bucket=bucket, error=str(exc))
        return False


def healthcheck() -> tuple[bool, str]:
    """Verify MinIO is reachable and the target bucket exists/creatable.

    Used by the production startup gate and ``/healthz``. Returns ``(ok, detail)``.
    ``ok=False`` with a reason when credentials are missing, the client lib is
    absent, or the server is unreachable."""
    if not enabled():
        return False, "MINIO_ACCESS_KEY/MINIO_SECRET_KEY not configured"
    bucket = _bucket()
    try:
        client = _client()
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def put_reference_photo(driver_id: str, jpeg: bytes) -> Optional[str]:
    """Store an approved reference frame and return its object URL.

    Returns ``None`` (caller keeps the base64 frame in the DB) if MinIO is not
    configured, the client is missing, or the upload fails.
    """
    if not jpeg or not enabled():
        return None
    bucket = _bucket()
    object_name = f"{driver_id}.jpg"
    try:
        client = _client()
        if not _ensure_bucket(client, bucket):
            return None
        client.put_object(
            bucket,
            object_name,
            data=io.BytesIO(jpeg),
            length=len(jpeg),
            content_type="image/jpeg",
        )
        url = f"{_public_base()}/{object_name}"
        log.info("reference_photo_stored", driver_id=driver_id, bytes=len(jpeg))
        return url
    except Exception as exc:  # noqa: BLE001
        log.warning("reference_photo_store_failed", driver_id=driver_id, error=str(exc))
        return None


__all__ = ["enabled", "healthcheck", "put_reference_photo"]
