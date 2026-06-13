"""MinIO persistence for the anomaly detector.

Two buckets:

  * ``models``  (prefix ``anomaly/``) — the trained trajectory-AE weights +
    metrics summary. ``upload_artifacts`` pushes them after ``/train_ae``;
    ``sync_weights`` pulls them on startup if absent locally so the API can serve
    a model trained in another container. Mirrors ai/congestion's approach.
  * ``evidence`` — the offending camera frame saved per alert as
    ``{alert_id}.jpg`` (see ``evidence.py``), required for the TFC-2 wrong-way
    scenario in Prompt 8.

All operations are best-effort: MinIO being down must never crash the service —
training/serving continues, evidence URLs are simply omitted.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from jnpa_shared.logging import get_logger

from .config import AnomalyConfig

log = get_logger("anomaly.storage")


def _client(cfg: AnomalyConfig):
    from minio import Minio  # lazy import

    return Minio(
        cfg.minio_endpoint,
        access_key=cfg.minio_access_key,
        secret_key=cfg.minio_secret_key,
        secure=cfg.minio_secure,
    )


def ensure_bucket(cfg: AnomalyConfig, bucket: str) -> bool:
    try:
        client = _client(cfg)
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            log.info("minio_bucket_created", bucket=bucket)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_unavailable", bucket=bucket, error=str(exc))
        return False


def upload_artifacts(cfg: AnomalyConfig) -> bool:
    """Push local AE weights + metrics to the models bucket. True on success."""
    if not ensure_bucket(cfg, cfg.minio_model_bucket):
        return False
    ok = True
    try:
        client = _client(cfg)
        weights = Path(cfg.weights_path)
        metrics = Path(cfg.metrics_path)
        if weights.is_file():
            client.fput_object(cfg.minio_model_bucket, cfg.weights_key, str(weights))
            log.info("minio_weights_uploaded", key=cfg.weights_key)
        else:
            ok = False
        if metrics.is_file():
            client.fput_object(
                cfg.minio_model_bucket, cfg.metrics_key, str(metrics),
                content_type="application/json",
            )
            log.info("minio_metrics_uploaded", key=cfg.metrics_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_upload_failed", error=str(exc))
        ok = False
    return ok


def sync_weights(cfg: AnomalyConfig) -> Optional[str]:
    """Pull AE weights/metrics from MinIO if absent locally. Returns local
    weights path if available after the sync, else None."""
    local = Path(cfg.weights_path)
    try:
        if not ensure_bucket(cfg, cfg.minio_model_bucket):
            return str(local) if local.is_file() else None
        client = _client(cfg)
        for key, dest in ((cfg.weights_key, local),
                          (cfg.metrics_key, Path(cfg.metrics_path))):
            if dest.is_file():
                continue
            try:
                client.stat_object(cfg.minio_model_bucket, key)
            except Exception:  # noqa: BLE001 - NoSuchKey etc.
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.fget_object(cfg.minio_model_bucket, key, str(dest))
            log.info("minio_artifact_downloaded", key=key, path=str(dest))
        return str(local) if local.is_file() else None
    except Exception as exc:  # noqa: BLE001
        log.warning("minio_sync_failed", error=str(exc))
        return str(local) if local.is_file() else None


def put_evidence(cfg: AnomalyConfig, object_name: str, jpeg: bytes) -> Optional[str]:
    """Store an evidence jpeg under ``evidence/{object_name}`` and return its URL.

    Returns ``None`` if MinIO is unavailable (the caller then attaches no URL).
    """
    import io

    if not jpeg:
        return None
    if not ensure_bucket(cfg, cfg.minio_evidence_bucket):
        return None
    try:
        client = _client(cfg)
        client.put_object(
            cfg.minio_evidence_bucket,
            object_name,
            data=io.BytesIO(jpeg),
            length=len(jpeg),
            content_type="image/jpeg",
        )
        url = f"{cfg.evidence_base}/{object_name}"
        log.info("evidence_stored", object=object_name, bytes=len(jpeg))
        return url
    except Exception as exc:  # noqa: BLE001
        log.warning("evidence_store_failed", object=object_name, error=str(exc))
        return None


__all__ = [
    "ensure_bucket",
    "upload_artifacts",
    "sync_weights",
    "put_evidence",
]
