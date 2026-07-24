"""Autoencoder training pipeline (POST /train_ae + CLI).

Builds a corpus of *normal* trajectories and trains the 1D-conv trajectory
autoencoder, then persists weights + metrics locally and to MinIO. The corpus is
assembled from, in order of preference:

  1. real ``core.truck_telemetry`` over the last ``ae_train_days`` days, grouped
     by ``device_id`` into per-trip tracks, and
  2. synthetic normal corridor tracks (always added) so training never starves on
     a fresh stack with no telemetry yet.

The anomaly threshold is the configured percentile (default 99th) of the
reconstruction error over the training corpus — set inside ``TrajectoryAutoencoder.train``.

Requires torch; if torch is unavailable the function returns a structured
"skipped" result rather than raising, so the service still runs rules-only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from jnpa_shared.logging import configure_logging, get_logger

from .autoencoder.features import batch_features
from .autoencoder.model import TrajectoryAutoencoder, write_metrics
from .config import AnomalyConfig
from . import storage, synthetic
from .types import Track, TrackPoint

log = get_logger("anomaly.train")


def _telemetry_tracks(cfg: AnomalyConfig, days: int) -> List[Track]:
    """Group recent ``core.truck_telemetry`` rows into per-device tracks."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception:  # noqa: BLE001
        return []
    since = datetime.now(tz=timezone.utc) - timedelta(days=days)
    rows: List[dict] = []
    try:
        with psycopg.connect(cfg.postgres_dsn_libpq, connect_timeout=3) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT device_id, plate, ts, lat, lon, speed_kmh, heading"
                    " FROM core.truck_telemetry"
                    " WHERE ts >= %s AND device_id IS NOT NULL"
                    " ORDER BY device_id, ts",
                    (since,),
                )
                rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.warning("telemetry_query_failed", error=str(exc))
        return []

    by_device: Dict[str, Track] = {}
    for r in rows:
        did = r["device_id"]
        track = by_device.get(did)
        if track is None:
            track = Track(track_id=f"TELE-{did}", device_id=did, plate=r.get("plate"))
            by_device[did] = track
        track.add(TrackPoint(
            ts=r["ts"], lat=float(r["lat"]), lon=float(r["lon"]),
            speed_kmh=float(r["speed_kmh"] or 0.0), heading=float(r["heading"] or 0.0),
        ))
    # Keep only tracks with enough points to define a trajectory.
    return [t for t in by_device.values() if len(t.points) >= 8]


def train_autoencoder(cfg: AnomalyConfig, days: int | None = None) -> dict:
    """Train + persist the AE. Returns a metrics dict (or a skip/error report)."""
    days = days if days is not None else cfg.ae_train_days

    if not TrajectoryAutoencoder.torch_available():
        log.warning("ae_train_skipped_no_torch")
        return {"status": "skipped", "reason": "torch_unavailable"}

    real = _telemetry_tracks(cfg, days)
    # Always blend in synthetic normal tracks so we hit ae_min_tracks and the AE
    # sees the canonical corridor manifold even on a fresh stack.
    synth_n = max(cfg.ae_min_tracks, 256)
    corpus = real + synthetic.normal_tracks(synth_n, seq_len=cfg.ae_seq_len, seed=cfg.ae_seed)

    if len(corpus) < cfg.ae_min_tracks:
        return {"status": "skipped", "reason": "insufficient_tracks",
                "tracks": len(corpus), "min_required": cfg.ae_min_tracks}

    feats = batch_features(corpus, cfg.ae_seq_len)
    ae = TrajectoryAutoencoder(cfg)
    metrics = ae.train(feats)
    metrics.update({
        "status": "ok",
        "real_tracks": len(real),
        "synthetic_tracks": synth_n,
        "trained_at": datetime.now(tz=timezone.utc).isoformat(),
        "ae_train_days": days,
    })

    ae.save()
    write_metrics(cfg, metrics)
    storage.upload_artifacts(cfg)
    log.info("ae_trained", **{k: metrics[k] for k in
                              ("n_tracks", "final_loss", "threshold")})
    return metrics


def main() -> None:  # pragma: no cover - CLI entrypoint
    cfg = AnomalyConfig.from_env()
    configure_logging(cfg.log_level)
    result = train_autoencoder(cfg)
    log.info("ae_train_result", **{k: v for k, v in result.items()
                                   if not isinstance(v, (list, dict))})


if __name__ == "__main__":  # pragma: no cover
    main()
