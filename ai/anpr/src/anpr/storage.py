"""MinIO weights persistence.

On startup the service ensures the ``models`` bucket exists and, if the YOLO
weights are present locally but absent in MinIO, uploads them; conversely, if
the local file is missing but the object exists in MinIO, it is pulled down.
This keeps fine-tuned/large artefacts out of the git repo while making the
service self-bootstrapping. All operations are best-effort — MinIO being down
must never crash the API (it just runs in degraded mode).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from jnpa_shared.logging import get_logger

from .config import AnprAiConfig

log = get_logger("anpr.storage")

# Object keys within the bucket.
WEIGHTS_KEY = "anpr/license_plate_detector.pt"
ADAPTER_PREFIX = "anpr/rec_indian/"


def _client(cfg: AnprAiConfig):
    from minio import Minio  # lazy import

    return Minio(
        cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        secure=cfg.minio_secure,
    )


def ensure_bucket(cfg: AnprAiConfig) -> bool:
    try:
        client = _client(cfg)
        if not client.bucket_exists(cfg.minio_bucket):
            client.make_bucket(cfg.minio_bucket)
            log.info("minio_bucket_created", bucket=cfg.minio_bucket)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_unavailable", error=str(exc))
        return False


def sync_weights(cfg: AnprAiConfig) -> Optional[str]:
    """Two-way reconcile the YOLO weights with MinIO. Returns the local path if
    weights are available after the sync, else None."""
    local = Path(cfg.yolo_weights)
    try:
        client = _client(cfg)
        if not ensure_bucket(cfg):
            return str(local) if local.is_file() else None

        exists_remote = True
        try:
            client.stat_object(cfg.minio_bucket, WEIGHTS_KEY)
        except Exception:  # noqa: BLE001  (NoSuchKey etc.)
            exists_remote = False

        if local.is_file() and not exists_remote:
            client.fput_object(cfg.minio_bucket, WEIGHTS_KEY, str(local))
            log.info("minio_weights_uploaded", key=WEIGHTS_KEY)
        elif exists_remote and not local.is_file():
            local.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(cfg.minio_bucket, WEIGHTS_KEY, str(local))
            log.info("minio_weights_downloaded", key=WEIGHTS_KEY, path=str(local))
        return str(local) if local.is_file() else None
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_sync_failed", error=str(exc))
        return str(local) if local.is_file() else None


__all__ = ["ensure_bucket", "sync_weights", "WEIGHTS_KEY", "ADAPTER_PREFIX"]
