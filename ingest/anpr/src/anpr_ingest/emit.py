"""Kafka emission of AnprRead events.

Builds `AnprRead` records from plate candidates and produces them as JSON to the
``anpr.reads`` topic via the shared kafka_io helper. In DRY_RUN mode the raw
crop is emitted (as a data URL in ``image_url``) with a synthetic plate token
and no OCR call. Otherwise the crop is POSTed to the AI ANPR service and the
returned plate/confidence are used.

Also emits the periodic ``no_feed`` health event when there are zero clips.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from jnpa_shared import kafka_io
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import AnprRead, VehicleClass

from .config import AnprConfig
from .detect import PlateCandidate
from .metrics import KAFKA_ERRORS, NO_FEED_EVENTS, PLATES_EMITTED

log = get_logger("anpr_ingest.emit")

NO_FEED_TOPIC = "anpr.reads"  # health events ride the same topic, flagged below


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Emitter:
    """Owns the Kafka producer and turns candidates into AnprRead events."""

    def __init__(self, cfg: AnprConfig) -> None:
        self.cfg = cfg
        # Point the shared producer at this service's broker config.
        self._producer = kafka_io.get_producer(
            {"bootstrap.servers": cfg.kafka_brokers, "client.id": "anpr-ingest"}
        )

    # -- internal -----------------------------------------------------------
    def _produce(self, record: AnprRead) -> bool:
        try:
            kafka_io.produce(self._producer, self.cfg.topic, record,
                             key=record.camera_id, flush=False)
            self._producer.poll(0)
            return True
        except Exception as exc:  # noqa: BLE001 (BufferError, KafkaException, ...)
            KAFKA_ERRORS.inc()
            log.warning("kafka_produce_failed", error=str(exc), camera_id=record.camera_id)
            return False

    async def _recognise(self, cand: PlateCandidate, client: httpx.AsyncClient) -> tuple[str, float]:
        """Call the AI ANPR service for a plate string + confidence."""
        try:
            resp = await client.post(
                self.cfg.ai_anpr_url,
                json={"camera_id": cand.camera_id, "image_b64": cand.crop_b64_jpeg()},
                timeout=self.cfg.ai_timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("plate", "UNKNOWN")), float(data.get("conf", 0.0))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("ai_anpr_call_failed", error=str(exc), camera_id=cand.camera_id)
            return "UNKNOWN", 0.0

    # -- public -------------------------------------------------------------
    def emit_dry_run(self, cand: PlateCandidate, ts: datetime, weather: str) -> Optional[AnprRead]:
        """DRY_RUN: emit the raw crop only (no OCR), with a placeholder plate."""
        b64 = cand.crop_b64_jpeg()
        record = AnprRead(
            ts=ts,
            camera_id=cand.camera_id,
            plate="DRYRUN-CROP",
            conf=round(cand.det_conf, 4),
            vehicle_class=cand.vehicle_class,
            image_url=f"data:image/jpeg;base64,{b64}" if b64 else None,
            weather=weather,
            degraded=cand.degraded,
        )
        if self._produce(record):
            PLATES_EMITTED.labels(camera_id=cand.camera_id).inc()
            return record
        return None

    async def emit_with_ai(
        self, cand: PlateCandidate, ts: datetime, weather: str, client: httpx.AsyncClient
    ) -> Optional[AnprRead]:
        """Non-DRY_RUN: OCR via the AI service, then emit the recognised plate."""
        plate, conf = await self._recognise(cand, client)
        record = AnprRead(
            ts=ts,
            camera_id=cand.camera_id,
            plate=plate,
            conf=conf,
            vehicle_class=cand.vehicle_class,
            image_url=None,  # AI service persists the snapshot (Prompt 3.1)
            weather=weather,
            degraded=cand.degraded,
        )
        if self._produce(record):
            PLATES_EMITTED.labels(camera_id=cand.camera_id).inc()
            return record
        return None

    def emit_no_feed(self) -> Optional[AnprRead]:
        """Emit a health heartbeat when there are zero clips to replay."""
        record = AnprRead(
            ts=_utcnow(),
            camera_id="no_feed",
            plate="NO_FEED",
            conf=0.0,
            vehicle_class=VehicleClass.UNKNOWN,
            image_url=None,
            weather="clear",
            degraded=True,
        )
        if self._produce(record):
            NO_FEED_EVENTS.inc()
            log.info("no_feed_health_event")
            return record
        return None

    def flush(self, timeout: float = 5.0) -> None:
        try:
            self._producer.flush(timeout)
        except Exception as exc:  # noqa: BLE001
            log.warning("kafka_flush_failed", error=str(exc))
