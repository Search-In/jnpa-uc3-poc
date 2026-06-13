"""Rolling feature windows per segment.

Turns a flat history (synthetic bootstrap rows and/or live ``traffic_snapshots``
joined with RFID/ANPR/trucking-derived counts) into the dense tensor the model
consumes:

    feature_tensor : (S, N, F)   S = number of 60-s steps, N = segments,
                                 F = len(FEATURE_NAMES) node features per step
    windows / labels via make_supervised(): sliding (window) -> onset label.

Per-step node features (F = 9), all scaled to roughly 0..1 so the GNN/LSTM
train stably without a separate scaler artefact:

    0 speed_norm          segment speed / free-flow speed
    1 jam_norm            jam_factor / 10
    2 rfid_norm           RFID reads in the window, squashed
    3 anpr_norm           ANPR reads in the window, squashed
    4 truck_speed_norm    trucking-app median speed / free-flow speed
    5 lane_norm           static: lanes / max_lanes
    6 signalised          static: 0/1
    7 tod_sin             time-of-day (IST) sine
    8 tod_cos             time-of-day (IST) cosine

Label (congestion onset): for a window ending at step *t*, the target for each
segment is 1 if that segment is congested at any step in the horizon window
``(t, t + horizon_steps]`` **and** was NOT already congested at step *t* — i.e.
the model predicts the *onset* of congestion in the next 15 minutes, not merely
that a segment is currently jammed. (If a segment is already congested at *t* it
is excluded from positives so we score genuine onset, which is what the bid
F1 target is about.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .config import CongestionConfig
from .graph import CorridorGraph
from .synthetic import HistoryRow

FEATURE_NAMES: Tuple[str, ...] = (
    "speed_norm",
    "jam_norm",
    "rfid_norm",
    "anpr_norm",
    "truck_speed_norm",
    "lane_norm",
    "signalised",
    "tod_sin",
    "tod_cos",
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _squash(x: np.ndarray, scale: float) -> np.ndarray:
    """Map a non-negative count to 0..1 with a saturating curve."""
    return 1.0 - np.exp(-x / scale)


@dataclass
class FeatureMatrix:
    """Dense per-step feature tensor plus the labels needed to congest-score."""

    times: List          # length S, tz-aware UTC datetimes (one per step)
    features: np.ndarray  # (S, N, F)
    congested: np.ndarray  # (S, N) bool — is the segment congested at this step
    segment_ids: List[str]

    @property
    def num_steps(self) -> int:
        return self.features.shape[0]


class FeatureBuilder:
    """Builds the dense feature tensor and supervised windows from history."""

    def __init__(self, cfg: CongestionConfig, graph: CorridorGraph) -> None:
        self.cfg = cfg
        self.graph = graph
        self.seg_index = {sid: i for i, sid in enumerate(graph.segment_ids)}
        self._lane_norm = np.array(
            [m.lane_count / graph.max_lanes for m in graph.meta], dtype=np.float32
        )
        self._signalised = np.array(
            [1.0 if m.signalised else 0.0 for m in graph.meta], dtype=np.float32
        )

    # ---------------------------------------------------------------- build
    def build_matrix(self, rows: Sequence[HistoryRow]) -> FeatureMatrix:
        """Pivot flat rows into (S, N, F). Rows are grouped by timestamp."""
        cfg = self.cfg
        n = self.graph.num_nodes
        free_v = cfg.free_flow_speed_kmh

        # Bucket rows by timestamp (stable, sorted) then by segment.
        by_ts: Dict = {}
        for r in rows:
            by_ts.setdefault(r.ts, {})[r.segment_id] = r
        times = sorted(by_ts.keys())
        s = len(times)

        feats = np.zeros((s, n, len(FEATURE_NAMES)), dtype=np.float32)
        congested = np.zeros((s, n), dtype=bool)

        for si, ts in enumerate(times):
            ts_ist = ts.astimezone(_IST)
            mins = ts_ist.hour * 60 + ts_ist.minute
            tod_sin = math.sin(2 * math.pi * mins / 1440.0)
            tod_cos = math.cos(2 * math.pi * mins / 1440.0)
            seg_rows = by_ts[ts]

            speeds = np.full(n, free_v, dtype=np.float32)
            jams = np.zeros(n, dtype=np.float32)
            rfid = np.zeros(n, dtype=np.float32)
            anpr = np.zeros(n, dtype=np.float32)
            tspeed = np.full(n, free_v, dtype=np.float32)

            for sid, r in seg_rows.items():
                i = self.seg_index.get(sid)
                if i is None:
                    continue
                speeds[i] = r.speed_kmh
                jams[i] = r.jam_factor
                rfid[i] = r.rfid_count
                anpr[i] = r.anpr_count
                tspeed[i] = r.truck_speed_kmh

            feats[si, :, 0] = np.clip(speeds / free_v, 0.0, 1.5)
            feats[si, :, 1] = np.clip(jams / 10.0, 0.0, 1.0)
            feats[si, :, 2] = _squash(rfid, scale=10.0)
            feats[si, :, 3] = _squash(anpr, scale=8.0)
            feats[si, :, 4] = np.clip(tspeed / free_v, 0.0, 1.5)
            feats[si, :, 5] = self._lane_norm
            feats[si, :, 6] = self._signalised
            feats[si, :, 7] = tod_sin
            feats[si, :, 8] = tod_cos

            congested[si] = (jams >= cfg.congest_jam_factor) | (speeds <= cfg.congest_speed_kmh)

        return FeatureMatrix(times=times, features=feats, congested=congested,
                             segment_ids=list(self.graph.segment_ids))

    # ---------------------------------------------------------------- supervise
    def make_supervised(
        self, fm: FeatureMatrix
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List]:
        """Sliding windows -> (X, Y, mask, window_end_times).

        X    : (M, T, N, F) input windows
        Y    : (M, N) onset labels (1 = congestion onsets within the horizon)
        mask : (M, N) 1 = score this (segment,window); 0 = excluded because the
               segment was already congested at the window end (not an onset).
        window_end_times : list of length M (tz-aware UTC) for the val split.
        """
        cfg = self.cfg
        t_win = cfg.window
        horizon_steps = max(1, int(cfg.horizon_min * 60 / cfg.aggregate_s))
        s, n, f = fm.features.shape

        X: List[np.ndarray] = []
        Y: List[np.ndarray] = []
        M: List[np.ndarray] = []
        ends: List = []

        last_start = s - t_win - horizon_steps
        for start in range(0, last_start + 1):
            end = start + t_win - 1  # index of the last step in the window
            window = fm.features[start : start + t_win]            # (T, N, F)
            now_cong = fm.congested[end]                            # (N,)
            future = fm.congested[end + 1 : end + 1 + horizon_steps]  # (h, N)
            onset = future.any(axis=0) & (~now_cong)                # (N,)
            mask = (~now_cong).astype(np.float32)                   # score onsets only
            X.append(window)
            Y.append(onset.astype(np.float32))
            M.append(mask)
            ends.append(fm.times[end])

        if not X:
            empty = np.zeros((0, t_win, n, f), dtype=np.float32)
            return empty, np.zeros((0, n), np.float32), np.zeros((0, n), np.float32), []

        return np.stack(X), np.stack(Y), np.stack(M), ends

    # ---------------------------------------------------------------- live
    def latest_window(self, fm: FeatureMatrix) -> Optional[np.ndarray]:
        """Return the most recent (T, N, F) window for inference, or None."""
        if fm.num_steps < self.cfg.window:
            return None
        return fm.features[-self.cfg.window :]


__all__ = ["FeatureBuilder", "FeatureMatrix", "FEATURE_NAMES"]
