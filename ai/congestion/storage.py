"""MinIO persistence for trained weights + metrics under bucket ``models``.

Mirrors ai/anpr's storage approach: best-effort, never crashes the service if
MinIO is down. Weights land at ``congestion/congestion_gnn_lstm.pt`` and the
metrics summary at ``congestion/metrics.json`` inside the ``models`` bucket.

  * ``upload_artifacts``  — push local weights + metrics to MinIO (train.py).
  * ``sync_weights``      — pull weights/metrics down if missing locally but
                            present remotely (infer.py startup), so the API can
                            serve a model trained in another container.
  * ``ensure_bucket``     — idempotent bucket create.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from jnpa_shared.logging import get_logger

from .config import CongestionConfig

log = get_logger("congestion.storage")


def _client(cfg: CongestionConfig):
    from minio import Minio  # lazy import

    return Minio(
        cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        secure=cfg.minio_secure,
    )


def ensure_bucket(cfg: CongestionConfig) -> bool:
    try:
        client = _client(cfg)
        if not client.bucket_exists(cfg.minio_bucket):
            client.make_bucket(cfg.minio_bucket)
            log.info("minio_bucket_created", bucket=cfg.minio_bucket)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_unavailable", error=str(exc))
        return False


def upload_artifacts(cfg: CongestionConfig) -> bool:
    """Push local weights + metrics to MinIO. Returns True on full success."""
    if not ensure_bucket(cfg):
        return False
    ok = True
    try:
        client = _client(cfg)
        weights = Path(cfg.weights_path)
        metrics = Path(cfg.metrics_path)
        if weights.is_file():
            client.fput_object(cfg.minio_bucket, cfg.weights_key, str(weights))
            log.info("minio_weights_uploaded", key=cfg.weights_key)
        else:
            ok = False
        if metrics.is_file():
            client.fput_object(
                cfg.minio_bucket, cfg.metrics_key, str(metrics),
                content_type="application/json",
            )
            log.info("minio_metrics_uploaded", key=cfg.metrics_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_upload_failed", error=str(exc))
        ok = False
    return ok


def sync_weights(cfg: CongestionConfig) -> Optional[str]:
    """Pull weights/metrics from MinIO if absent locally. Returns local weights
    path if available after the sync, else None."""
    local = Path(cfg.weights_path)
    try:
        if not ensure_bucket(cfg):
            return str(local) if local.is_file() else None
        client = _client(cfg)
        for key, dest in ((cfg.weights_key, local),
                          (cfg.metrics_key, Path(cfg.metrics_path))):
            if dest.is_file():
                continue
            try:
                client.stat_object(cfg.minio_bucket, key)
            except Exception:  # noqa: BLE001 - NoSuchKey etc.
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(cfg.minio_bucket, key, str(dest))
            log.info("minio_artifact_downloaded", key=key, path=str(dest))
        return str(local) if local.is_file() else None
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_sync_failed", error=str(exc))
        return str(local) if local.is_file() else None


__all__ = ["ensure_bucket", "upload_artifacts", "sync_weights"]
