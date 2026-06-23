"""Best-effort evidence persistence to MinIO/S3 for anpr-ingest.

Mirrors ai/anomaly's evidence pattern (``anomaly/storage.py:put_evidence``): a
plate crop is stored under ``{object_name}`` in the evidence bucket and the
resolved object URL is returned for ``AnprRead.image_url``. This replaces the
DRY_RUN base64 data-URL with a real object-store URL on the live path.

All operations are best-effort: MinIO being unavailable must never crash the
ingest. The caller then attaches no URL (``image_url=None``), exactly as before.
"""
from __future__ import annotations

import io
from typing import Optional

from jnpa_shared.logging import get_logger

from .config import AnprConfig

log = get_logger("anpr_ingest.evidence")


class EvidenceStore:
    """Lazy MinIO client wrapper. The client and bucket are created on first use
    so an unconfigured/unreachable MinIO degrades gracefully (no URL) instead of
    failing service startup."""

    def __init__(self, cfg: AnprConfig) -> None:
        self.cfg = cfg
        self._client = None
        self._bucket_ok = False

    def _client_or_none(self):
        if self._client is not None:
            return self._client
        try:
            from minio import Minio  # lazy import — optional dependency
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence_minio_unimportable", error=str(exc))
            return None
        self._client = Minio(
            self.cfg.minio_endpoint,
            access_key=self.cfg.minio_access_key,
            secret_key=self.cfg.minio_secret_key,
            secure=self.cfg.minio_secure,
        )
        return self._client

    def _ensure_bucket(self) -> bool:
        if self._bucket_ok:
            return True
        client = self._client_or_none()
        if client is None:
            return False
        try:
            if not client.bucket_exists(self.cfg.evidence_bucket):
                client.make_bucket(self.cfg.evidence_bucket)
            self._bucket_ok = True
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence_minio_unavailable", error=str(exc))
            return False

    def _base_url(self) -> str:
        if self.cfg.evidence_base_url:
            return self.cfg.evidence_base_url.rstrip("/")
        scheme = "https" if self.cfg.minio_secure else "http"
        return f"{scheme}://{self.cfg.minio_endpoint}/{self.cfg.evidence_bucket}"

    def put(self, object_name: str, jpeg: bytes) -> Optional[str]:
        """Store ``jpeg`` under ``object_name`` and return its object URL, or None
        when evidence is disabled / the crop is empty / MinIO is unavailable."""
        if not self.cfg.evidence_enabled or not jpeg:
            return None
        if not self._ensure_bucket():
            return None
        client = self._client_or_none()
        if client is None:
            return None
        try:
            client.put_object(
                self.cfg.evidence_bucket,
                object_name,
                data=io.BytesIO(jpeg),
                length=len(jpeg),
                content_type="image/jpeg",
            )
            url = f"{self._base_url()}/{object_name}"
            log.info("evidence_stored", object=object_name, bytes=len(jpeg))
            return url
        except Exception as exc:  # noqa: BLE001
            log.warning("evidence_store_failed", object=object_name, error=str(exc))
            return None


__all__ = ["EvidenceStore"]
