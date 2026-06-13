"""Shared camera frame bus over Redis Streams.

A lightweight pub/sub for jpeg-encoded camera frames so multiple consumers
(``ai/anomaly`` for behavioural anomaly detection, and later ``ai/anpr``) can
read the same feed that ``ingest/anpr`` produces, without each re-decoding the
source clips.

Design
------
* One Redis Stream per camera, keyed ``frames.{camera_id}`` (see ``stream_key``).
* Each entry carries the raw jpeg bytes plus a few small fields (camera_id,
  timestamp, sequence). Frames are binary, so the client here is created with
  ``decode_responses=False`` (the shared ``redis_io`` client decodes to str and
  would corrupt jpeg bytes — hence a dedicated binary client).
* The producer ``XADD``s with ``maxlen`` so the stream is trimmed to the last N
  entries (default 600 ≈ 2 min @ 5 fps) to bound memory.
* Consumers tail with ``XREAD BLOCK`` from the last id they saw (``$`` for "only
  new frames"), so a late-starting anomaly detector does not replay stale frames.

All operations are best-effort: Redis being unavailable must never crash the
producer (frames are simply not mirrored) — it just logs and continues. This is
synchronous (redis-py sync client) because the producer side runs the encode +
publish in a worker thread (``asyncio.to_thread``) and consumers run their own
blocking read loop in a thread too; that keeps the hot CPU paths off the event
loop without dragging async Redis into the per-frame path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, List, Optional, Tuple

from .config import get_settings
from .logging import get_logger

log = get_logger("jnpa_shared.frame_bus")

STREAM_PREFIX = "frames"
DEFAULT_MAXLEN = 600           # last ~600 frames per camera (≈2 min @ 5 fps)
DEFAULT_BLOCK_MS = 1000        # XREAD block timeout


def stream_key(camera_id: str) -> str:
    """Redis Stream key for a camera, e.g. ``frames.CAM-COR-01``."""
    return f"{STREAM_PREFIX}.{camera_id}"


@dataclass
class FrameMessage:
    """One decoded frame-bus entry."""

    entry_id: str          # Redis stream entry id, e.g. "1718200000000-0"
    camera_id: str
    ts: datetime
    seq: int
    jpeg: bytes            # raw jpeg-encoded frame bytes

    @property
    def size_bytes(self) -> int:
        return len(self.jpeg)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class FrameBusProducer:
    """Publishes jpeg frames to per-camera Redis Streams (best-effort)."""

    def __init__(self, url: Optional[str] = None, maxlen: int = DEFAULT_MAXLEN) -> None:
        self.url = url or get_settings().redis_url
        self.maxlen = maxlen
        self._client = None
        self._seq: dict[str, int] = {}

    def _ensure_client(self):
        if self._client is None:
            import redis  # lazy import (sync client, binary-safe)

            # decode_responses MUST be False: frames are raw jpeg bytes.
            self._client = redis.from_url(self.url, decode_responses=False)
        return self._client

    def publish(self, camera_id: str, jpeg: bytes, ts: Optional[datetime] = None) -> Optional[str]:
        """XADD a jpeg frame to ``frames.{camera_id}``; trims to ``maxlen``.

        Returns the new entry id, or ``None`` if Redis was unavailable (the call
        never raises so a frame-bus outage cannot stall the ingest pipeline).
        """
        ts = ts or _utcnow()
        seq = self._seq.get(camera_id, 0) + 1
        self._seq[camera_id] = seq
        try:
            client = self._ensure_client()
            entry_id = client.xadd(
                stream_key(camera_id),
                {
                    b"camera_id": camera_id.encode("utf-8"),
                    b"ts": ts.isoformat().encode("utf-8"),
                    b"seq": str(seq).encode("utf-8"),
                    b"jpeg": jpeg,
                },
                maxlen=self.maxlen,
                approximate=True,
            )
            return entry_id.decode("utf-8") if isinstance(entry_id, bytes) else str(entry_id)
        except Exception as exc:  # noqa: BLE001 - never let the bus stall ingest
            log.debug("frame_publish_failed", camera_id=camera_id, error=str(exc))
            return None

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


class FrameBusConsumer:
    """Tails one or more per-camera Redis Streams for new jpeg frames."""

    def __init__(
        self,
        camera_ids: List[str],
        url: Optional[str] = None,
        block_ms: int = DEFAULT_BLOCK_MS,
        start: str = "$",
    ) -> None:
        self.url = url or get_settings().redis_url
        self.camera_ids = list(camera_ids)
        self.block_ms = block_ms
        self._client = None
        # Per-stream last-seen id; "$" means "only frames added after we start".
        self._last: dict[str, str] = {stream_key(c): start for c in self.camera_ids}

    def _ensure_client(self):
        if self._client is None:
            import redis  # lazy import

            self._client = redis.from_url(self.url, decode_responses=False)
        return self._client

    def _parse(self, key: str, entry_id: str, fields: dict) -> FrameMessage:
        def _s(name: str, default: str = "") -> str:
            v = fields.get(name.encode("utf-8")) or fields.get(name)
            if isinstance(v, bytes):
                return v.decode("utf-8")
            return v if v is not None else default

        camera_id = _s("camera_id") or key.split(".", 1)[-1]
        ts_raw = _s("ts")
        try:
            ts = datetime.fromisoformat(ts_raw) if ts_raw else _utcnow()
        except ValueError:
            ts = _utcnow()
        seq_raw = _s("seq", "0")
        jpeg = fields.get(b"jpeg") or fields.get("jpeg") or b""
        if isinstance(jpeg, str):
            jpeg = jpeg.encode("latin-1")
        return FrameMessage(
            entry_id=entry_id,
            camera_id=camera_id,
            ts=ts,
            seq=int(seq_raw) if seq_raw.isdigit() else 0,
            jpeg=jpeg,
        )

    def read(self, count: int = 16) -> List[FrameMessage]:
        """Blocking read of up to ``count`` new frames per stream. May return []."""
        client = self._ensure_client()
        try:
            resp = client.xread(self._last, count=count, block=self.block_ms)
        except Exception as exc:  # noqa: BLE001
            log.debug("frame_read_failed", error=str(exc))
            return []
        out: List[FrameMessage] = []
        for stream_key_raw, entries in resp or []:
            key = stream_key_raw.decode("utf-8") if isinstance(stream_key_raw, bytes) else stream_key_raw
            for entry_id_raw, fields in entries:
                entry_id = entry_id_raw.decode("utf-8") if isinstance(entry_id_raw, bytes) else entry_id_raw
                self._last[key] = entry_id
                out.append(self._parse(key, entry_id, fields))
        return out

    def stream(self) -> Iterator[FrameMessage]:
        """Generator yielding frames forever (caller breaks out to stop)."""
        while True:
            for msg in self.read():
                yield msg

    def latest(self, camera_id: str) -> Optional[Tuple[str, FrameMessage]]:
        """Fetch the single most-recent frame on a camera stream (XREVRANGE).

        Used by the evidence pipeline to grab the offending frame for an alert
        without consuming the live tail. Returns ``(entry_id, FrameMessage)`` or
        ``None`` if the stream is empty / Redis is down.
        """
        client = self._ensure_client()
        try:
            rows = client.xrevrange(stream_key(camera_id), count=1)
        except Exception as exc:  # noqa: BLE001
            log.debug("frame_latest_failed", camera_id=camera_id, error=str(exc))
            return None
        if not rows:
            return None
        entry_id_raw, fields = rows[0]
        entry_id = entry_id_raw.decode("utf-8") if isinstance(entry_id_raw, bytes) else entry_id_raw
        return entry_id, self._parse(stream_key(camera_id), entry_id, fields)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


__all__ = [
    "STREAM_PREFIX",
    "DEFAULT_MAXLEN",
    "stream_key",
    "FrameMessage",
    "FrameBusProducer",
    "FrameBusConsumer",
]
