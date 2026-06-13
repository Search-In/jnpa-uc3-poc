"""Per-track trajectory feature extraction for the autoencoder.

Each track is resampled to a fixed-length sequence of ``ae_seq_len`` steps and
turned into a ``(seq_len, n_features)`` matrix:

  * **speed** — the (normalised) speed series, capturing stop/go and cruise.
  * **heading** — encoded as ``sin`` and ``cos`` of the bearing so the wrap-around
    at 360°→0° is continuous (a raw degree series has a discontinuity there).
  * implicitly, the **dwell pattern** falls out of the speed series: long runs of
    near-zero speed are exactly the looping/idling signatures the AE must learn.

The matrix is returned channel-last ``(seq_len, 3)``; the model transposes to
``(channels, seq_len)`` for the 1D convs. Resampling to a fixed length lets one
model handle tracks of any duration and makes the reconstruction error
comparable across tracks.

This module is pure numpy (no torch) so feature building works on a bare host
and inside the synthetic test fixtures.
"""
from __future__ import annotations

import math
from typing import List

import numpy as np

from ..types import Track

N_FEATURES = 3  # speed_norm, heading_sin, heading_cos
# Speed used to normalise the speed channel into ~[0, 1.2] (corridor design speed).
_SPEED_NORM_KMH = 80.0


def _resample(series: List[float], n: int) -> np.ndarray:
    """Linearly resample a 1-D series to exactly ``n`` samples."""
    arr = np.asarray(series, dtype=np.float64)
    if arr.size == 0:
        return np.zeros(n, dtype=np.float64)
    if arr.size == 1:
        return np.full(n, arr[0], dtype=np.float64)
    xp = np.linspace(0.0, 1.0, num=arr.size)
    x = np.linspace(0.0, 1.0, num=n)
    return np.interp(x, xp, arr)


def track_features(track: Track, seq_len: int) -> np.ndarray:
    """Build a ``(seq_len, N_FEATURES)`` feature matrix for one track."""
    speeds = track.speed_series()
    headings = track.heading_series()

    speed_rs = _resample(speeds, seq_len) / _SPEED_NORM_KMH
    head_rad = [math.radians(h) for h in headings]
    sin_rs = _resample([math.sin(r) for r in head_rad], seq_len)
    cos_rs = _resample([math.cos(r) for r in head_rad], seq_len)

    feats = np.stack([speed_rs, sin_rs, cos_rs], axis=-1)  # (seq_len, 3)
    return feats.astype(np.float32)


def batch_features(tracks: List[Track], seq_len: int) -> np.ndarray:
    """Stack per-track feature matrices into ``(N, seq_len, N_FEATURES)``."""
    if not tracks:
        return np.zeros((0, seq_len, N_FEATURES), dtype=np.float32)
    return np.stack([track_features(t, seq_len) for t in tracks], axis=0)


__all__ = ["N_FEATURES", "track_features", "batch_features"]
