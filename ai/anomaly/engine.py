"""Anomaly engine: run the rule engine + autoencoder over tracks, emit alerts.

The engine is the hub the live tracker, the telemetry path, and the tests all
feed tracks into. For each track it:

  1. runs every rule (wrong-way, abandoned, illegal-parking, route-deviation),
  2. scores the track's trajectory features against the trained autoencoder
     (ANOMALOUS_TRAJECTORY when reconstruction error exceeds the threshold),
  3. dedupes against recently-emitted alerts (one alert per (track, kind) within
     a cooldown window, so a track that stays wrong-way for 10 s does not spam),
  4. attaches the offending frame as MinIO evidence, and
  5. emits via the sink (Postgres + Kafka).

Rules are pure functions returning ``Alert | None``; the engine owns all the
stateful concerns (dedup, evidence, sinks) so the rules stay trivially testable.
``evaluate_track`` returns the alerts it emitted, which the tests assert on
directly (they pass a no-op sink + evidence writer).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

from jnpa_shared.logging import get_logger
from jnpa_shared.schemas import Alert

from .autoencoder.features import track_features
from .autoencoder.model import TrajectoryAutoencoder
from .config import AnomalyConfig
from .rules import abandoned, parking, route_deviation, wrongway
from .types import Track

log = get_logger("anomaly.engine")

LatLon = Tuple[float, float]
KIND_ANOMALOUS_TRAJECTORY = "ANOMALOUS_TRAJECTORY"

# Per-(track_id, kind) cooldown so a sustained condition emits once per window.
_DEFAULT_COOLDOWN_S = 60.0


class AnomalyEngine:
    """Stateful orchestrator around the pure rules + the AE."""

    def __init__(
        self,
        cfg: AnomalyConfig,
        sink=None,
        evidence=None,
        autoencoder: Optional[TrajectoryAutoencoder] = None,
        cooldown_s: float = _DEFAULT_COOLDOWN_S,
    ) -> None:
        self.cfg = cfg
        self.sink = sink
        self.evidence = evidence
        self.ae = autoencoder
        self.cooldown_s = cooldown_s
        # (track_id, kind) -> last-emitted ts, for dedup.
        self._last_emit: Dict[Tuple[str, str], datetime] = {}

    # -- public API ---------------------------------------------------------
    def evaluate_track(
        self,
        track: Track,
        *,
        route: Optional[Sequence[LatLon]] = None,
        jpeg: Optional[bytes] = None,
        emit: bool = True,
    ) -> List[Alert]:
        """Run all rules + the AE on one track; emit + return surviving alerts.

        ``route`` is the truck's assigned polyline (route-deviation only — skipped
        if absent). ``jpeg`` is the offending frame for evidence (else the
        evidence writer falls back to the frame bus). ``emit=False`` runs the
        detectors without sinking (used by callers that batch their own emission).
        """
        candidates: List[Alert] = []

        ww = wrongway.evaluate(track, self.cfg)
        if ww is not None:
            candidates.append(ww)

        # Parking and abandoned are mutually exclusive (zone vs non-zone), so at
        # most one of these fires; running both is harmless and order-independent.
        pk = parking.evaluate(track, self.cfg)
        if pk is not None:
            candidates.append(pk)
        ab = abandoned.evaluate(track, self.cfg)
        if ab is not None:
            candidates.append(ab)

        if route:
            rd = route_deviation.evaluate(track, route, self.cfg)
            if rd is not None:
                candidates.append(rd)

        # The autoencoder is the catch-all for behaviours the rules *cannot
        # enumerate*. If a rule already explained this track, suppress the AE
        # alert: a wrong-way vehicle is also an "anomalous trajectory", but the
        # specific WRONG_WAY alert is the actionable one — emitting both is noise.
        if not candidates:
            ae_alert = self._score_autoencoder(track)
            if ae_alert is not None:
                candidates.append(ae_alert)

        emitted: List[Alert] = []
        for alert in candidates:
            if not self._should_emit(track.track_id, alert):
                continue
            if emit:
                self._finalize(alert, jpeg)
            emitted.append(alert)
        return emitted

    def evaluate_tracks(self, tracks: Sequence[Track]) -> List[Alert]:
        """Convenience: evaluate many tracks (no per-track route/evidence)."""
        out: List[Alert] = []
        for t in tracks:
            out.extend(self.evaluate_track(t))
        return out

    # -- autoencoder --------------------------------------------------------
    def _score_autoencoder(self, track: Track) -> Optional[Alert]:
        if self.ae is None or not self.ae.loaded:
            return None
        # Need enough points for a meaningful trajectory.
        if len(track.points) < 4:
            return None
        feats = track_features(track, self.cfg.ae_seq_len)[None, ...]
        results = self.ae.score_batch(feats)
        if not results:
            return None
        res = results[0]
        if not res.is_anomalous:
            return None
        last = track.latest
        severity = "critical" if res.ratio >= 2.0 else "warning"
        return Alert(
            kind=KIND_ANOMALOUS_TRAJECTORY,
            severity=severity,
            plate=track.plate,
            payload={
                "track_id": track.track_id,
                "camera_id": track.camera_id,
                "device_id": track.device_id,
                "recon_error": round(res.error, 6),
                "threshold": round(res.threshold, 6),
                "error_ratio": res.ratio,
                "lat": last.lat if last else None,
                "lon": last.lon if last else None,
                "ts": last.ts.isoformat() if last else None,
            },
        )

    # -- dedup + emit -------------------------------------------------------
    def _should_emit(self, track_id: str, alert: Alert) -> bool:
        key = (track_id, alert.kind)
        last = self._last_emit.get(key)
        now = alert.ts
        if last is not None and (now - last).total_seconds() < self.cooldown_s:
            return False
        self._last_emit[key] = now
        return True

    def _finalize(self, alert: Alert, jpeg: Optional[bytes]) -> None:
        # Surface camera as the alert's gate_id-free locator; gate alerts may set
        # gate_id when the camera maps to a gate (left to downstream enrichment).
        if self.evidence is not None:
            try:
                self.evidence.attach(alert, jpeg)
            except Exception as exc:  # noqa: BLE001
                log.warning("evidence_attach_failed", kind=alert.kind, error=str(exc))
        if self.sink is not None:
            self.sink.emit(alert)
        log.info("alert_emitted", kind=alert.kind, severity=alert.severity,
                 track_id=alert.payload.get("track_id"),
                 evidence=bool(alert.payload.get("evidence_url")))

    def prune(self, older_than_s: float = 3600.0) -> None:
        """Drop stale dedup entries to bound memory."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(seconds=older_than_s)
        self._last_emit = {k: v for k, v in self._last_emit.items() if v >= cutoff}


__all__ = ["AnomalyEngine", "KIND_ANOMALOUS_TRAJECTORY"]
