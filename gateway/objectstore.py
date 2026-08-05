"""MinIO persistence for driver-enrolment reference photos (Identity / C2).

A driver's approved reference frame is stored in the ``driver-enrolment`` bucket
as ``{driver_id}.jpg`` and an object URL is returned for the enrollment record.

All operations are best-effort and degrade gracefully: if the ``minio`` client is
absent or MinIO is unreachable, ``put_reference_photo`` returns ``None`` and the
caller keeps the base64 frame in Postgres instead — the enrollment still completes.
Mirrors ai/anomaly/storage.py's approach (lazy import, never crash the request).
"""
from __future__ import annotations

import io
import os
from datetime import timedelta
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from .logging import get_logger

log = get_logger("gateway.objectstore")


def _endpoint() -> str:
    return os.environ.get("MINIO_ENDPOINT", "minio:9000").strip()


def _secure() -> bool:
    return os.environ.get("MINIO_SECURE", "false").strip().lower() in {
        "1", "true", "yes", "on"}


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


def _client(endpoint: Optional[str] = None, secure: Optional[bool] = None):
    from minio import Minio  # lazy import — optional dependency

    return Minio(
        endpoint or _endpoint(),
        access_key=os.environ.get("MINIO_ACCESS_KEY", "").strip(),
        secret_key=os.environ.get("MINIO_SECRET_KEY", "").strip(),
        secure=_secure() if secure is None else secure,
    )


def _public_endpoint_parts() -> tuple[str, str]:
    """Split ``MINIO_PUBLIC_ENDPOINT`` into ``(host[:port], url_path_prefix)``.

    The browser-reachable S3 host may sit behind a reverse-proxy sub-path, e.g.
    ``traffic-three.searchintech.in/minio``. The MinIO SDK forbids a path in its
    endpoint (it must be host-only), and a SigV4 presigned URL signs the *path*
    MinIO ultimately receives. So we sign against the bare host and re-insert the
    sub-path into the returned URL afterwards; nginx strips ``/minio`` again
    before forwarding, so the signed path (``/bucket/key``) still matches.

    Returns ``("", "")`` when no public endpoint is configured (caller falls back
    to the in-network endpoint). Tolerates a leading scheme and stray slashes.
    """
    raw = os.environ.get("MINIO_PUBLIC_ENDPOINT", "").strip()
    if not raw:
        return "", ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.strip("/")
    host, _, path = raw.partition("/")
    prefix = "/" + path.strip("/") if path else ""
    return host, prefix


def _public_secure() -> bool:
    secure_env = os.environ.get("MINIO_PUBLIC_SECURE", "").strip().lower()
    if secure_env:
        return secure_env in {"1", "true", "yes", "on"}
    return _secure()


def _presign_client(public_host: str):
    """MinIO client whose endpoint is the *browser-reachable* host.

    Presigned URLs sign the host, so the endpoint used to mint them must be the
    host the browser will call. In-network MinIO is ``minio:9000`` (not resolvable
    from a browser); ``MINIO_PUBLIC_ENDPOINT`` supplies the public host (its
    sub-path, if any, is spliced back on separately — see ``_public_endpoint_parts``).
    Falls back to the in-network endpoint when no public host is configured.
    """
    if not public_host:
        return _client()
    return _client(endpoint=public_host, secure=_public_secure())


def _parse_s3_url(url: str) -> tuple[str, str]:
    """Split ``s3://bucket/key/with/slashes`` into ``(bucket, key)``."""
    parts = urlsplit(url)
    bucket = parts.netloc
    key = parts.path.lstrip("/")
    return bucket, key


def _apply_path_prefix(url: str, prefix: str) -> str:
    """Insert a reverse-proxy sub-path (e.g. ``/minio``) ahead of the URL path."""
    if not prefix:
        return url
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, prefix + parts.path, parts.query, parts.fragment))


def resolve_photo_url(photo: Optional[str], *, expires: int = 3600) -> Optional[str]:
    """Turn a stored photo pointer into something a browser can load.

    - ``s3://bucket/key`` -> a time-limited presigned GET URL (browser-reachable).
    - already ``http(s)://`` or a ``data:`` URI -> returned unchanged.
    - empty / ``None`` -> ``None``.

    Best-effort: if MinIO is not configured or presigning fails, the original
    value is returned so the caller degrades rather than crashing the request.
    """
    if not photo:
        return None
    if not photo.startswith("s3://"):
        return photo
    if not enabled():
        return photo
    try:
        bucket, key = _parse_s3_url(photo)
        if not bucket or not key:
            return photo
        public_host, prefix = _public_endpoint_parts()
        client = _presign_client(public_host)
        url = client.presigned_get_object(
            bucket, key, expires=timedelta(seconds=expires))
        return _apply_path_prefix(url, prefix)
    except Exception as exc:  # noqa: BLE001
        log.warning("presign_failed", photo=photo, error=str(exc))
        return photo


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


__all__ = ["enabled", "healthcheck", "put_reference_photo", "resolve_photo_url"]
