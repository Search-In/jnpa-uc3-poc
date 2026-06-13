"""Evidence pipeline: save the offending frame for every alert.

On each alert the detector grabs the offending camera frame and stores it to
MinIO under ``evidence/{alert_id}.jpg``, attaching the resulting URL to
``alert.payload["evidence_url"]``. This is required for the TFC-2 wrong-way
scenario in Prompt 8 (the operator needs to see the vehicle that triggered the
alert).

The frame is sourced, in order of preference:
  1. an explicit ``jpeg`` passed by the caller (e.g. the exact frame ByteTrack
     was processing when it raised the alert), else
  2. the most-recent frame on the alert's camera stream via the frame bus
     (``FrameBusConsumer.latest``).

Best-effort: if no frame is available or MinIO is down, the alert is still
emitted — just without an ``evidence_url``.
"""
from __future__ import annotations

from typing import Optional
from uuid import UUID

from jnpa_shared.frame_bus import FrameBusConsumer
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import Alert

from .config import AnomalyConfig
from . import storage

log = get_logger("anomaly.evidence")


class EvidenceWriter:
    """Captures and persists the offending frame for alerts."""

    def __init__(self, cfg: AnomalyConfig) -> None:
        self.cfg = cfg
        # A consumer used only for `latest()` snapshots (no tailing).
        self._bus: Optional[FrameBusConsumer] = None

    def _bus_consumer(self) -> FrameBusConsumer:
        if self._bus is None:
            self._bus = FrameBusConsumer([], url=self.cfg.redis_url)
        return self._bus

    def _frame_for_alert(self, alert: Alert, jpeg: Optional[bytes]) -> Optional[bytes]:
        if jpeg:
            return jpeg
        camera_id = alert.payload.get("camera_id")
        if not camera_id:
            return None
        latest = self._bus_consumer().latest(camera_id)
        if latest is None:
            return None
        return latest[1].jpeg

    def attach(self, alert: Alert, jpeg: Optional[bytes] = None) -> Alert:
        """Save the offending frame and attach ``evidence_url`` to the alert.

        Returns the same alert (mutated in place). The object key is the alert's
        UUID so it is unique and trivially joinable back to the alert row.
        """
        frame = self._frame_for_alert(alert, jpeg)
        if not frame:
            log.debug("evidence_no_frame", alert_id=str(alert.id), kind=alert.kind)
            return alert
        object_name = f"{_alert_key(alert.id)}.jpg"
        url = storage.put_evidence(self.cfg, object_name, frame)
        if url:
            alert.payload["evidence_url"] = url
            alert.payload["evidence_object"] = object_name
        return alert

    def close(self) -> None:
        if self._bus is not None:
            self._bus.close()
            self._bus = None


def _alert_key(alert_id: UUID) -> str:
    return str(alert_id)


__all__ = ["EvidenceWriter"]
