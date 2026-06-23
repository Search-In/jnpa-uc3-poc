"""Kafka emission of AnprRead events.

Builds `AnprRead` records from plate candidates and produces them as JSON to the
``anpr.reads`` topic via the shared kafka_io helper. In DRY_RUN mode the raw
crop is emitted (as a data URL in ``image_url``) with a synthetic plate token
and no OCR call. Otherwise the crop is POSTed to the AI ANPR service and the
returned plate/confidence are used.

Also emits the periodic ``no_feed`` health event when there are zero clips.
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from typing import Optional

import httpx

from jnpa_shared import kafka_io
from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import (
    AnprRead,
    Condition,
    VehicleClass,
    ocr_confidence_for_condition,
)

from .config import AnprConfig
from .detect import PlateCandidate
from .evidence_store import EvidenceStore
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
        # Seeded RNG for the DRY_RUN OCR-confidence draw, so the per-condition
        # distribution (≥95% in CLEAR, degraded in FOG/NIGHT) replays identically
        # under the same global SEED.
        self._ocr_rng = random.Random(getattr(cfg, "seed", 1337))
        # Evidence object store for the live path (replaces the DRY_RUN base64
        # data-URL with a real MinIO/S3 object URL). Best-effort.
        self._evidence = EvidenceStore(cfg)

    # -- internal -----------------------------------------------------------
    def _produce(self, record: AnprRead, raw_ref: Optional[str] = None) -> bool:
        try:
            # DRY_RUN replay is synthetic (SIM); a live camera feed is LIVE.
            source_system = "SIM" if getattr(self.cfg, "dry_run", True) else "LIVE"
            kafka_io.produce(
                self._producer, self.cfg.topic, record,
                key=record.camera_id, flush=False,
                event_type="jnpa.anpr.detection",
                source_system=source_system,
                raw_ref=raw_ref,
            )
            self._producer.poll(0)
            return True
        except Exception as exc:  # noqa: BLE001 (BufferError, KafkaException, ...)
            KAFKA_ERRORS.inc()
            log.warning("kafka_produce_failed", error=str(exc), camera_id=record.camera_id)
            return False

    async def _recognise(
        self, jpeg: bytes, cand: PlateCandidate, client: httpx.AsyncClient
    ) -> tuple[str, float]:
        """Call the AI ANPR + OCR service (ai/anpr ``POST /infer``) for a plate
        string + confidence. The crop is sent as a multipart JPEG image; the
        service returns ``{plate, conf, bbox, valid, ...}``."""
        try:
            if not jpeg:
                return "UNKNOWN", 0.0
            resp = await client.post(
                self.cfg.ai_anpr_url,
                files={"image": ("plate.jpg", jpeg, "image/jpeg")},
                timeout=self.cfg.ai_timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data.get("plate", "UNKNOWN")), float(data.get("conf", 0.0))
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            log.warning("ai_anpr_call_failed", error=str(exc), camera_id=cand.camera_id)
            return "UNKNOWN", 0.0

    # -- public -------------------------------------------------------------
    def emit_dry_run(
        self,
        cand: PlateCandidate,
        ts: datetime,
        weather: str,
        condition: Condition = Condition.CLEAR,
    ) -> Optional[AnprRead]:
        """DRY_RUN: emit the raw crop only (no OCR), with a placeholder plate.

        Confidence is drawn from the per-condition OCR distribution (seeded) so
        the demo shows ≥95% in CLEAR and graceful degradation in FOG/NIGHT,
        instead of echoing the raw detector score.
        """
        b64 = cand.crop_b64_jpeg()
        conf = round(ocr_confidence_for_condition(condition.value, self._ocr_rng), 4)
        record = AnprRead(
            ts=ts,
            camera_id=cand.camera_id,
            plate="DRYRUN-CROP",
            conf=conf,
            vehicle_class=cand.vehicle_class,
            image_url=f"data:image/jpeg;base64,{b64}" if b64 else None,
            weather=weather,
            condition=condition,
            # A low-confidence read in poor conditions is itself a (soft) degrade.
            degraded=cand.degraded or conf < 0.90,
        )
        raw_ref = f"clip://{cand.camera_id}#box={cand.box}&ts={ts.isoformat()}"
        if self._produce(record, raw_ref=raw_ref):
            PLATES_EMITTED.labels(camera_id=cand.camera_id).inc()
            return record
        return None

    async def emit_with_ai(
        self, cand: PlateCandidate, ts: datetime, weather: str, client: httpx.AsyncClient
    ) -> Optional[AnprRead]:
        """Non-DRY_RUN: persist the crop to the evidence store, OCR via the AI
        service, then emit the recognised plate with a real object-store URL."""
        jpeg = cand.crop_jpeg_bytes()
        # Store the crop in MinIO/S3 and link the read to its object URL. The
        # upload runs in a worker thread so the (blocking) MinIO put stays off the
        # event loop. Best-effort: a failed/disabled store yields image_url=None.
        object_name = f"anpr/{cand.camera_id}/{ts.strftime('%Y%m%dT%H%M%S%f')}.jpg"
        evidence_url = await asyncio.to_thread(self._evidence.put, object_name, jpeg)
        plate, conf = await self._recognise(jpeg, cand, client)
        record = AnprRead(
            ts=ts,
            camera_id=cand.camera_id,
            plate=plate,
            conf=conf,
            vehicle_class=cand.vehicle_class,
            image_url=evidence_url,  # MinIO/S3 object URL (None if store unavailable)
            weather=weather,
            degraded=cand.degraded,
        )
        raw_ref = f"clip://{cand.camera_id}#box={cand.box}&ts={ts.isoformat()}"
        if self._produce(record, raw_ref=raw_ref):
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
